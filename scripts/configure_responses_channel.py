#!/usr/bin/env python3
"""Optional startup config for a generic Responses -> Chat upstream bridge.

Runs inside the new-api container on boot (via responses_entrypoint.sh). Waits
for new-api to become healthy, logs in with an existing root administrator,
and creates or updates one advancedcustom channel (type 58) using the built-in
openai_responses_to_openai_chat_completions converter.

If the admin creds env vars are UNSET, this is a no-op — a manually
UI-configured new-api is left untouched. Re-running updates the named channel
in place, so channel identity and availability are preserved across boots.

Env vars (set in Zeabur):
    NEWAPI_ADMIN_USER       admin username; presence (with _PASSWORD) enables auto-config
    NEWAPI_ADMIN_PASSWORD   admin password
    RESPONSES_UPSTREAM_BASE_URL   upstream OpenAI-compatible /v1 base URL
    RESPONSES_UPSTREAM_API_KEY    upstream credential
    RESPONSES_MODELS              comma-separated upstream model ids
    RESPONSES_CHANNEL_NAME        default responses-chat-bridge
    RESPONSES_SELF_USE      default true (set false to skip enabling self-use)
    NEWAPI_INTERNAL_URL     default http://localhost:3000

The script deliberately has no vendor-specific defaults and never creates or
elevates users. Deployment credentials and commercial routing choices remain
outside this open-source repository.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

INTERNAL_URL = os.environ.get("NEWAPI_INTERNAL_URL", "http://localhost:3000").rstrip("/")
ADMIN_USER = (os.environ.get("NEWAPI_ADMIN_USER") or "").strip()
ADMIN_PASS = (os.environ.get("NEWAPI_ADMIN_PASSWORD") or "").strip()
UPSTREAM_BASE_URL = (os.environ.get("RESPONSES_UPSTREAM_BASE_URL") or "").strip()
UPSTREAM_API_KEY = (os.environ.get("RESPONSES_UPSTREAM_API_KEY") or "").strip()
CHANNEL_NAME = os.environ.get("RESPONSES_CHANNEL_NAME", "responses-chat-bridge").strip()
SELF_USE = os.environ.get("RESPONSES_SELF_USE", "true").strip().lower() in ("1", "true", "yes")
MODELS = [m.strip() for m in os.environ.get("RESPONSES_MODELS", "").split(",") if m.strip()]
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


def get_session_token():
    """Login with an existing root user; never mutate account privileges."""
    tok = login()
    if not tok:
        return None
    _, data = req("GET", "/api/user/self", None, tok)
    if (data.get("data") or {}).get("role") != 100:
        print("configured administrator is not root; refusing auto-config.", file=sys.stderr)
        return None
    return tok


def main():
    if not ADMIN_USER or not ADMIN_PASS:
        print("NEWAPI_ADMIN_USER/PASSWORD unset — skipping responses channel "
              "auto-config (manual UI config left untouched).")
        return 0
    if not UPSTREAM_BASE_URL or not UPSTREAM_API_KEY or not MODELS:
        print("auto-config requires RESPONSES_UPSTREAM_BASE_URL, "
              "RESPONSES_UPSTREAM_API_KEY, and RESPONSES_MODELS; skipping.", file=sys.stderr)
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
    existing = next(
        (c for c in items if c.get("name") == CHANNEL_NAME and c.get("type") == 58),
        None,
    )

    settings = {"advanced_custom": {"advanced_routes": [{
        "incoming_path": "/v1/responses",
        "upstream_path": UPSTREAM_BASE_URL.rstrip("/") + "/chat/completions",
        "converter": CONVERTER,
        "models": MODELS,
    }]}}
    channel = {
        "name": CHANNEL_NAME, "type": 58, "key": UPSTREAM_API_KEY,
        "base_url": UPSTREAM_BASE_URL, "models": ",".join(MODELS),
        "group": "default", "groups": ["default"], "settings": json.dumps(settings),
    }
    if existing:
        channel["id"] = existing["id"]
        s, d = req("PUT", "/api/channel/", channel, tok)
        action = "update"
    else:
        s, d = req("POST", "/api/channel/", {"channel": channel, "mode": "single"}, tok)
        action = "create"
    print(f"{action} channel {CHANNEL_NAME}: http={s} success={d.get('success')} "
          f"model_count={len(MODELS)} msg={d.get('message')}")
    if not d.get("success"):
        print("channel configuration failed; inspect new-api server logs for details.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
