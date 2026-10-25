#!/usr/bin/env bash
# Capture the A2 report screenshots into docs/screenshots/a2/ with headless Chrome.
#
#   scripts/capture_a2_screenshots.sh [minutes] [end] [request_id]
#     minutes     time window length (default 10); end: window end for `date -d` (default 20 s ago)
#     request_id  the ID to search for in Kibana (default: the latest trace-* ID in the api logs)
#
# Same approach as scripts/capture_screenshots.sh (A1): a fixed absolute window, never "now".
set -euo pipefail
cd "$(dirname "$0")/.."
MINUTES="${1:-10}"
END="${2:-20 seconds ago}"
RID="${3:-$(docker compose logs api --no-log-prefix 2>/dev/null | grep -o '"request_id": "trace-[0-9]*"' | tail -1 | cut -d'"' -f4)}"
OUT=docs/screenshots/a2
PROFILE="$(mktemp -d)"
trap 'rm -rf "$PROFILE"' EXIT
TO=$(( $(date -d "$END" +%s) * 1000 ))
FROM=$(( TO - MINUTES * 60 * 1000 ))
G="orgId=1&theme=light&from=$FROM&to=$TO"
ISO() { date -u -d "@$(( $1 / 1000 ))" +%Y-%m-%dT%H:%M:%S.000Z; }
mkdir -p "$OUT"
echo "window: $(ISO $FROM) -> $(ISO $TO), kibana request_id: $RID"

shot() {  # url file width,height [budget ms]
  timeout 120 google-chrome --headless=new --disable-gpu --hide-scrollbars --no-first-run \
    --no-default-browser-check --user-data-dir="$PROFILE" --window-size="$3" \
    --virtual-time-budget="${4:-15000}" --screenshot="$OUT/$2" "$1" >/dev/null 2>&1
  echo "  $OUT/$2 ($(stat -c %s "$OUT/$2") bytes)"
}

# Grafana panels: pass a window that covers only the traffic run (e.g. 3 minutes), so earlier
# experiments (3 s injected delays) don't dominate the latency scale.
for id in 31 32 33 34 35 36; do
  shot "http://localhost:3000/d-solo/sensor-service?$G&panelId=$id" "c_panel_$id.png" 1000,340
done
shot "http://localhost:9090/targets" c_prometheus_targets.png 1400,880
shot "http://localhost:8000/docs#/default/create_reading_readings_post" a_swagger_post_readings.png 1300,2400 40000
KQ="request_id:\"$RID\""
shot "http://localhost:5601/app/discover#/?_g=(time:(from:'$(ISO $FROM)',to:'$(ISO $TO)'))&_a=(columns:!(service,event,transport,rpc_method,grpc_code,status_code,reading_id),dataSource:(dataViewId:sensor-logs,type:dataView),query:(language:kuery,query:'${KQ//\"/%22}'))" \
  b_kibana_trace_both_services.png 1600,900 40000
