#!/usr/bin/env bash
# Rate-limit demo: 7 readings for one device as fast as possible, then wait for Retry-After and send one more.
# The limit is per device: a burst of 5, refilled at 1 per second. Stop the simulator first for clean numbers
# (it uses other devices, so it doesn't share this device's bucket either way).
set -euo pipefail
URL=${URL:-http://localhost:8000}
DEV=${DEV:-dev-901}
post() {
  local ts; ts=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
  curl -s -o /tmp/rl_body -D /tmp/rl_head -w '%{http_code}' -X POST "$URL/readings" \
    -H 'Content-Type: application/json' -H "X-Request-ID: rl-$1" \
    -d "{\"device_id\":\"$DEV\",\"timestamp\":\"$ts\",\"temperature_c\":22,\"humidity_pct\":45,\"battery_pct\":88}"
}
for i in 1 2 3 4 5 6 7; do
  code=$(post "$i")
  ra=$(grep -i '^retry-after:' /tmp/rl_head | tr -d '\r' | cut -d' ' -f2 || true)
  echo "$(date +%T.%3N) request $i -> $code${ra:+  Retry-After: $ra}"
done
echo "--- 429 body:"; cat /tmp/rl_body; echo
echo "--- sleeping ${ra:-1} s (Retry-After)"; sleep "${ra:-1}"
echo "$(date +%T.%3N) request 8 -> $(post 8)"
