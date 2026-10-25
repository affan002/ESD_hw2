#!/usr/bin/env bash
# The user task over HTTP: a valid reading, the same reading repeated, a conflicting
# reading (same device and timestamp, different values) and an invalid one.
set -euo pipefail
URL=${URL:-http://localhost:8000}
TS=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
show() {  # $1 = label, $2 = JSON body
  echo "### $1"
  curl -s -i -X POST "$URL/readings" -H 'Content-Type: application/json' -H "X-Request-ID: ex-$RANDOM" -d "$2" \
    | grep -iE '^(HTTP/|content-type|location|idempotent-replayed|x-request-id)|^\{' | tr -d '\r'
  echo
}
VALID="{\"device_id\":\"dev-902\",\"timestamp\":\"$TS\",\"temperature_c\":22.5,\"humidity_pct\":45,\"battery_pct\":88}"
show "1. valid reading" "$VALID"
show "2. same request repeated (a retry)" "$VALID"
show "3. same device and timestamp, different temperature" "${VALID/22.5/30}"
show "4. invalid: humidity 150 % and a bad device id" "{\"device_id\":\"sensor-1\",\"timestamp\":\"$TS\",\"temperature_c\":22.5,\"humidity_pct\":150,\"battery_pct\":88}"
