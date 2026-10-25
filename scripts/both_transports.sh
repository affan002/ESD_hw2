#!/usr/bin/env bash
# The same task through both transports: one success and one failure each, then one
# reading sent over HTTP and repeated over gRPC with the same request ID, to show the
# shared database (gRPC replays it) and correlation (one ID in both services' logs).
set -euo pipefail
cd "$(dirname "$0")/.."
URL=${URL:-http://localhost:8000}
PY=${PY:-.venv/bin/python}
TS=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
RUN=$(date +%H%M%S)
http() {  # $1 request id, $2 body
  curl -s -o /tmp/bt_body -w 'HTTP %{http_code}' -X POST "$URL/readings" -H 'Content-Type: application/json' \
    -H "X-Request-ID: $1" -d "$2"; echo "  $(cut -c1-170 /tmp/bt_body)"
}
echo "### HTTP success";  http "b-http-ok-$RUN"  "{\"device_id\":\"dev-904\",\"timestamp\":\"$TS\",\"temperature_c\":22.5,\"humidity_pct\":45,\"battery_pct\":88}"
echo "### HTTP failure";  http "b-http-bad-$RUN" "{\"device_id\":\"dev-904\",\"timestamp\":\"$TS\",\"temperature_c\":22.5,\"humidity_pct\":150,\"battery_pct\":88}"
echo "### gRPC success";  $PY -m clients.grpc_caller --device dev-905 --request-id "b-grpc-ok-$RUN" || true
echo "### gRPC failure";  $PY -m clients.grpc_caller --device dev-905 --humidity 150 --request-id "b-grpc-bad-$RUN" || true
TS2=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
echo "### one request ID across both: HTTP stores the reading, gRPC repeats it (request_id=trace-$RUN)"
http "trace-$RUN" "{\"device_id\":\"dev-906\",\"timestamp\":\"$TS2\",\"temperature_c\":22.5,\"humidity_pct\":45,\"battery_pct\":88}"
$PY -m clients.grpc_caller --device dev-906 --timestamp "$TS2" --request-id "trace-$RUN" || true
echo "### logs for request_id=trace-$RUN"
sleep 1
docker compose logs api grpc --no-log-prefix 2>/dev/null | grep "\"trace-$RUN\"" | "$PY" -c '
import sys, json
for line in sys.stdin:
    d = json.loads(line)
    keep = {k: d[k] for k in ("transport", "reading_id", "status_code", "rpc_method", "grpc_code") if k in d}
    print(" ", d["timestamp"][11:], d["service"].ljust(11), d["event"].ljust(16), keep)' | sort
