#!/usr/bin/env python3
"""Idempotent startup-time config of the Responses -> Chat advancedcustom channel.

Runs inside the new-api container on boot (via responses_entrypoint.sh). Waits
for new-api to become healthy, then — ONLY if NEWAPI_ADMIN_USER +
NEWAPI_ADMIN_PASSWORD are set — logs in (registering + root-elevating the user
on first run), ensures the dxkp advancedcustom channel exists (type 58,
converter openai_responses_to_openai_chat_completions, route /v1/responses ->
dxkp /v1/chat/completions) with RESPONSES_MODELS, and enables self-use mode.

If the admin creds env vars are UNSET, this is a no-op — a manually
UI-configured new-api is left untouched. Re-running is safe (the named channel
is deleted and recreated), so this is fine to run on every boot.

Env vars (set in Zeabur):
    NEWAPI_ADMIN_USER       admin username; presence (with _PASSWORD) enables auto-config
    NEWAPI_ADMIN_PASSWORD   admin password
    NEWAPI_ADMIN_EMAIL      default <user>@local.dev (only used if registering)
    DXKP_BASE_URL           default https://ai.dxkp.com/v1
    DXKP_API_KEY            dxkp API key (required if auto-config enabled)
    RESPONSES_MODELS        comma-separated dxkp model ids (default the 6, with -A)
    RESPONSES_CHANNEL_NAME  default dxkp-responses
    RESPONSES_SELF_USE      default true (set false to skip enabling self-use)
    NEWAPI_INTERNAL_URL     default http://localhost:3000
    NEWAPI_DB_PATH          default /data/one-api.db (sqlite, for root elevation)
"""
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

INTERNAL_URL = os.environ.get("NEWAPI_INTERNAL_URL", "http://localhost:3000").rstrip("/")
ADMIN_USER = (os.environ.get("NEWAPI_ADMIN_USER") or "").strip()
ADMIN_PASS = (os.environ.get("NEWAPI_ADMIN_PASSWORD") or "").strip()
ADMIN_EMAIL = (os.environ.get("NEWAPI_ADMIN_EMAIL") or (ADMIN_USER + "@local.dev")).strip()
DXKP_BASE_URL = os.environ.get("DXKP_BASE_URL", "https://ai.dxkp.com/v1").strip()
DXKP_API_KEY = (os.environ.get("DXKP_API_KEY") or "").strip()
CHANNEL_NAME = os.environ.get("RESPONSES_CHANNEL_NAME", "dxkp-responses").strip()
SELF_USE = os.environ.get("RESPONSES_SELF_USE", "true").strip().lower() in ("1", "true", "yes")
DB_PATH = os.environ.get("NEWAPI_DB_PATH", "/data/one-api.db")
DEFAULT_MODELS = "DeepSeek-V4-Flash,DeepSeek-V4-Pro,GLM-5.2-A,Kimi-K2.6-A,MiniMax-M3-A,Qwen3.7-Max-A"
MODELS = [m.strip() for m in os.environ.get("RESPONSES_MODELS", DEFAULT_MODELS).split(",") if m.strip()]
CONVERTER = "openai_responses_to_openai_chat_completions"


def req(method, path, body=None, token=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(INTERNAL_URL + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}")
        except Exception:
            return e.code, {"_raw": raw[:200]}


def wait_healthy():
    for _ in range(90):
        try:
            s, _ = req("GET", "/api/status", timeout=3)
            if s == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def login():
    s, d = req("POST", "/api/user/login", {"username": ADMIN_USER, "password": ADMIN_PASS})
    if s == 200 and d.get("success"):
        return (d.get("data") or {}).get("access_token") or (d.get("data") or {}).get("token")
    return None


def register():
    s, d = req("POST", "/api/user/register",
               {"username": ADMIN_USER, "password": ADMIN_PASS, "email": ADMIN_EMAIL})
    return bool(d.get("success"))


def elevate_to_root():
    """Set the admin user's role=100 (root) directly in sqlite. Safe to repeat."""
    try:
        con = sqlite3.connect(DB_PATH, timeout=10)
        cur = con.cursor()
        cur.execute("UPDATE users SET role=100 WHERE username=?", (ADMIN_USER,))
        con.commit()
        con.close()
        return True
    except Exception as e:
        print(f"sqlite elevation failed: {e}", file=sys.stderr)
        return False


def get_session_token():
    """Login; if the user is missing/non-root, bootstrap it. Returns a root
    session token, or None on hard failure."""
    tok = login()
    if tok:
        s, d = req("GET", "/api/user/self", None, tok)
        role = (d.get("data") or {}).get("role")
        if role == 100:
            return tok
        # exists but not root -> elevate + re-login
        print(f"admin user role={role}, elevating to root via sqlite...")
        if elevate_to_root():
            return login()
        return None
    # login failed: maybe user doesn't exist -> register + elevate + login
    print("login failed; attempting register...")
    if register():
        print("registered; elevating to root...")
        elevate_to_root()
        return login()
    return None


def main():
    if not ADMIN_USER or not ADMIN_PASS:
        print("NEWAPI_ADMIN_USER/PASSWORD unset — skipping responses channel "
              "auto-config (manual UI config left untouched).")
        return 0
    if not DXKP_API_KEY:
        print("auto-config enabled but DXKP_API_KEY unset — cannot create channel; skipping.",
              file=sys.stderr)
        return 0

    print(f"waiting for new-api healthy at {INTERNAL_URL} ...")
    if not wait_healthy():
        print("new-api did not become healthy within 90s; skipping.", file=sys.stderr)
        return 0

    tok = get_session_token()
    if not tok:
        print("could not obtain a root admin session (check NEWAPI_ADMIN_USER/PASSWORD); skipping.",
              file=sys.stderr)
        return 0
    print("admin session established.")

    if SELF_USE:
        s, d = req("PUT", "/api/option/", {"key": "SelfUseModeEnabled", "value": "true"}, tok)
        print(f"self-use mode: http={s} success={d.get('success')}")

    s, d = req("GET", "/api/channel/?p=0&page_size=200", None, tok)
    items = ((d.get("data") or {}).get("items")) or []
    for c in items:
        if c.get("name") == CHANNEL_NAME and c.get("type") == 58:
            req("DELETE", f"/api/channel/{c['id']}", None, tok)
            print(f"deleted existing channel id={c['id']} ({CHANNEL_NAME})")

    settings = {"advanced_custom": {"advanced_routes": [{
        "incoming_path": "/v1/responses",
        "upstream_path": DXKP_BASE_URL.rstrip("/") + "/chat/completions",
        "converter": CONVERTER,
        "models": MODELS,
    }]}}
    channel = {
        "name": CHANNEL_NAME, "type": 58, "key": DXKP_API_KEY,
        "base_url": DXKP_BASE_URL, "models": ",".join(MODELS),
        "group": "default", "groups": ["default"], "settings": json.dumps(settings),
    }
    s, d = req("POST", "/api/channel/", {"channel": channel, "mode": "single"}, tok)
    print(f"create channel {CHANNEL_NAME}: http={s} success={d.get('success')} "
          f"models={','.join(MODELS)} msg={d.get('message')}")
    if not d.get("success"):
        print(json.dumps(d)[:300], file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
