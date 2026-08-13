#!/bin/sh
# new-api entrypoint wrapper: start new-api, then run the (idempotent, opt-in)
# Responses->Chat channel auto-config. new-api serves immediately; the config
# script waits for health itself and is a no-op when NEWAPI_ADMIN_TOKEN is unset.
/new-api &
PID=$!
python3 /app/configure_responses_channel.py 2>&1 | sed 's/^/[responses-config] /' \
  || echo "[responses-config] configure failed (non-fatal)"
wait $PID
