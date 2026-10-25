# IoT Sensor Ingestion Service

This service receives readings from simulated temperature, humidity and battery sensors. It validates each reading, stores it and flags anomalies. It's the base application for an observability assignment (Prometheus/Grafana metrics and Filebeat → Elasticsearch → Kibana logs).

> **Assignment 2** (HTTP APIs, gRPC, metrics) extends the Assignment 1 project (tag `a1-submission`). New in A2: one RFC 9457 error format, idempotent `POST /readings`, a per-device rate limit, a committed OpenAPI document, Python callers with timeout/retry/circuit breaker, a gRPC `SubmitReading` service in its own container, and gRPC metrics in Prometheus/Grafana. See [Assignment 2](#assignment-2) below and [REPORT.md](REPORT.md). Design decisions: [docs/DECISIONS.md](docs/DECISIONS.md).

## Prerequisites

- Docker Engine with the Compose v2 plugin (`docker compose version`).
- Your user must be able to talk to Docker without `sudo`. If you get `permission denied ... docker.sock`, run `sudo usermod -aG docker $USER`, then log out and back in.
- Python 3.12. This is only needed to run the tests locally.
- `curl`. `jq` is optional but useful.
- Free ports on the host: **8000** (API and dashboard), **50051** (gRPC), **9090** (Prometheus), **3000** (Grafana), **9200** (Elasticsearch) and **5601** (Kibana). **All of them are bound to 127.0.0.1**, so they're reachable only from this machine, not from your network: there's no login on `/admin/faults`, Grafana or Elasticsearch. Node Exporter listens on port **9100** of Docker's bridge address (172.17.0.1) only.
- To open the UIs from another device, change the `127.0.0.1:` prefixes in `docker-compose.yml`, but only on a network you trust.
- About **3.5 GB of free RAM** for the whole stack: Kibana uses about 1.7 GB and Elasticsearch about 1 GB. If you're short on memory, `docker compose stop kibana` frees about 1.7 GB, and logs keep flowing into Elasticsearch without it.
- About 6 GB of disk for the images. Elasticsearch needs `vm.max_map_count` ≥ 262144 (`cat /proc/sys/vm/max_map_count`); Ubuntu 24.04's default of 1048576 is fine.

## Start

Build the image and start everything: the API (which also serves the dashboard), the 20-device simulator, Prometheus, Node Exporter, Grafana, Elasticsearch, Kibana, Filebeat and the one-shot `logs-setup` job. The first start downloads about 6 GB of images, and later starts take about 2 minutes because Kibana is slow to boot:

```bash
docker compose up -d --build
```

All services should be up, with `api`, `elasticsearch` and `kibana` marked `(healthy)`, and `logs-setup` shown as `Exited (0)` (it runs once and stops):

```bash
docker compose ps
```

```bash
curl -s localhost:8000/health
```

The health check should return `{"status":"ok","db":"ok"}`.

- **Dashboard:** http://localhost:8000
- **Interactive API docs:** http://localhost:8000/docs
- **Grafana:** http://localhost:3000 (no login needed to view)
- **Prometheus:** http://localhost:9090
- **Kibana:** http://localhost:5601 (log search; no login)
- **Elasticsearch:** http://localhost:9200

After about 3 minutes, the device list shows the misbehaving devices:

```bash
curl -s localhost:8000/devices | jq -c '.devices[] | {device_id, status}'
```

dev-017 and dev-018 show `anomaly`. dev-020 turns `stale` for about 30 s of every 2 minutes.

To start only the API, with no simulated traffic or monitoring, run `docker compose up -d --build api`.

Readings are stored in SQLite at `/data/sensors.db`, inside the `sensor-data` Docker volume. The data survives `docker compose down` and restarts; only `docker compose down -v` deletes it. When the API runs without Docker, the database is `./data/sensors.db`.

## Assignment 2

Everything below runs against the stack started above. The callers run on the host with the project's virtualenv (see [Automated tests](#automated-tests) to create it).

| What | Command |
|---|---|
| Tests (113, incl. gRPC in-process) | `.venv/bin/pytest -q` |
| Regenerate the gRPC code from `proto/sensor_ingest.proto` | `scripts/gen_proto.sh` |
| Re-export the OpenAPI document (`docs/openapi.json`) after changing routes | `.venv/bin/python scripts/export_openapi.py` |
| Valid / repeated / conflicting / invalid reading over HTTP | `scripts/http_examples.sh` |
| Rate limit: burst of 7 for one device → `429` + `Retry-After` | `scripts/rate_limit_demo.sh` |
| HTTP caller, several calls | `.venv/bin/python -m clients.http_caller --calls 5` |
| HTTP caller demo: success → retry → breaker open → recovery (uses `/admin/faults`; stop the simulator first) | `docker compose stop simulator && .venv/bin/python -m clients.http_caller --demo; docker compose start simulator` |
| gRPC caller (input from flags, 2 s deadline) | `.venv/bin/python -m clients.grpc_caller --device dev-903 --temperature 40` |
| gRPC failure (INVALID_ARGUMENT) | `.venv/bin/python -m clients.grpc_caller --device dev-903 --humidity 150` |
| Success + failure through both transports, and one request ID in both services' logs | `scripts/both_transports.sh` |
| Repeatable traffic for the Grafana row *Submit a reading: HTTP vs gRPC* (~90 s) | `.venv/bin/python scripts/a2_traffic.py --rounds 90` |
| Screenshots for the report | `scripts/capture_a2_screenshots.sh` |

- **OpenAPI:** the document is [docs/openapi.json](docs/openapi.json); the live copy is http://localhost:8000/openapi.json and Swagger UI is http://localhost:8000/docs. `tests/test_openapi.py` fails if the committed file drifts from the code.
- **gRPC** listens on `127.0.0.1:50051` (no TLS, local only). Prometheus scrapes its metrics as job `sensor-grpc` (`grpc:9101`, inside the Compose network only).
- **Errors** from the HTTP API are `application/problem+json`: `{type, title, status, detail, instance, request_id, errors?}`.

## Use

### Dashboard

Open http://localhost:8000. The page refreshes every 5 s, and **Pause** stops it. It shows:

- **Summary cards:** device counts by status, and the total number of readings stored.
- **Devices table:** each device's status, latest values (the flagged ones highlighted), battery level, when it was last seen and its counters. You can filter it by status. Click a row to open that device's detail panel.
- **Device detail:** first and last seen, the latest anomaly reasons, and charts of the last 50 temperature, humidity and battery readings. Dots on a chart mark readings flagged for that value.
- **Recent anomalies:** the newest anomalies across the whole fleet. Click a device ID to open it.
- **Send a test reading:** presets for Normal, Too hot, Low battery and Impossible humidity. It shows whether the reading was stored, stored as an anomaly, or rejected with the reason, plus the request ID so you can find the request in the logs.
- **Fault injection:** turn on injected delay or errors for `POST /readings`. A yellow banner appears across the top while any fault is active, and it has a button to turn the fault off.

The dashboard is plain HTML, CSS and JavaScript in `app/static/`, served by the API itself. There's no build step. It only calls the public JSON API, and its requests carry `X-Request-ID: ui-…`.

### Dashboards (Grafana)

Open http://localhost:3000. No login is needed to view. It opens on the **Sensor Ingestion Service** dashboard, and the **Host (Node Exporter)** dashboard is under *Dashboards → Sensor Ingestion*. Both refresh automatically and show the last 15 minutes by default. Hover over a panel's ⓘ icon to see what it shows.

To go straight to a dashboard:
- App: http://localhost:3000/d/sensor-service
- Host: http://localhost:3000/d/node-host
- Add `?kiosk` to either URL for a full-screen view.

The datasource and dashboards are **provisioned from files** in `monitoring/grafana/`, so they come back identically after `docker compose down -v`. Changes made in the UI can't be saved. To change a dashboard, edit its JSON in `monitoring/grafana/dashboards/`, and Grafana reloads it within 30 s. To make edits in the UI and export them, log in as `admin` / `admin` (Grafana asks you to change the password on first login).

The architecture diagram is Mermaid text, `docs/diagrams/architecture.mmd`. After editing it, re-render the SVG and PNG with headless Chrome (this fetches Mermaid from jsDelivr once):

```bash
scripts/render_diagram.sh
```

To regenerate the report's screenshots (headless Chrome over a fixed time window; the stack must be running), capture the last 30 minutes, or pass an explicit end time:

```bash
scripts/capture_screenshots.sh 30
```

The images are written to `docs/screenshots/`.

### Metrics (Prometheus)

`docker compose up -d --build` also starts Prometheus, which scrapes the API, Node Exporter and itself every 5 s.

- **Raw metrics from the app:** `curl -s localhost:8000/metrics | grep '^sensor_'`
- **Prometheus UI:** http://localhost:9090. Use *Query* to run PromQL, and *Status → Target health* to see scrape targets. All three targets (`sensor-api`, `node`, `prometheus`) should be `UP`.

Some queries to try in the UI:

| What | PromQL |
|---|---|
| Readings per second, by outcome | `sum by (outcome) (rate(sensor_readings_total[1m]))` |
| p95 latency of `POST /readings` (seconds) | `histogram_quantile(0.95, sum by (le) (rate(sensor_http_request_duration_seconds_bucket{route="/readings"}[1m])))` |
| Devices by status | `sensor_devices` |
| Anomalies per second, by reason | `sum by (reason) (rate(sensor_anomalies_total[1m]))` |

Or query from the terminal:

```bash
curl -s --get localhost:9090/api/v1/query --data-urlencode 'query=sensor_devices' | jq -c '.data.result[] | {status: .metric.status, value: .value[1]}'
```

**Host metrics:** the `node-exporter` service reports CPU, memory, disk and network for the machine running Docker. It uses the host network and listens only on Docker's bridge address, so from this machine it's at http://172.17.0.1:9100/metrics (not `localhost`). Try `node_load1`, or `node_memory_MemAvailable_bytes / 1024^3`, in the Prometheus UI.

Prometheus keeps its data in the `prometheus-data` volume for 7 days. `docker compose down -v` deletes it along with the app's data.

### Logs (Filebeat → Elasticsearch → Kibana)

The API and the simulator write one JSON object per line to stdout. **Filebeat** reads those lines from Docker's log files, parses them into fields and sends them to **Elasticsearch** (data stream `sensor-logs`). **Kibana** searches them. The configuration is all files: `monitoring/filebeat/filebeat.yml`, `monitoring/elasticsearch/*.json`, `monitoring/kibana/saved-objects.ndjson` and `monitoring/logs-setup.sh`. The `logs-setup` job applies them on every `up`.

**Search in Kibana.** Open http://localhost:5601/app/discover. It opens on the **Sensor logs** data view. Set the time range (top right), then type a query (KQL) in the search bar:

| Find | KQL |
|---|---|
| Everything for one request | `request_id : "<id>"`. The ID is in the `X-Request-ID` response header, or shown by the dashboard's *Send a test reading* box |
| Warnings and errors | `level : ("ERROR" or "WARNING")` |
| Only errors (server failures, crashes) | `level : "ERROR"`. Normally there are none. To produce some, switch on the error fault for 30 s (see *Inject and undo a fault*), then turn it off |
| Why readings were rejected | `event : "reading_rejected"` (add the columns `errors.field` and `errors.type`) |
| Slow requests | `event : "http_request" and duration_ms > 100` |
| One device | `device_id : "dev-019"` |

Seven saved searches are under **Discover → Open** (the folder icon): *Errors and warnings*, *Server errors (5xx)*, *Slow requests*, *Rejected readings*, *Anomalies*, *Fault injection* and *Simulator send failures*.

**Or search from the terminal.** Count the stored log documents:

```bash
curl -s localhost:9200/sensor-logs/_count | jq .count
```

Find every log line for one request (replace `my-id-1` with your request ID):

```bash
curl -s localhost:9200/sensor-logs/_search -H 'Content-Type: application/json' -d '{"query":{"term":{"request_id":"my-id-1"}}}' | jq -c '.hits.hits[]._source | {"@timestamp", level, event, message}'
```

Remember that logs older than about an hour have been deleted by retention (below), so search for something recent.

**Retention.** Logs are kept for about 1 hour: the ILM policy `sensor-logs-policy` starts a new backing index every 10 minutes and deletes each one an hour after that. This is deliberately short so you can watch it happen. To see the backing indices:

```bash
curl -s 'localhost:9200/_cat/indices/.ds-sensor-logs*?v&h=index,docs.count,creation.date.string&s=index'
```

**Filebeat's own status**, useful when logs stop appearing:

```bash
docker compose logs filebeat --no-log-prefix | jq -r 'select(.["log.level"]!="info") | .message' | tail
```

### Experiments (Part E)

**E1: fault injection with a steady load.** Stop the simulator, so the only `POST /readings` traffic is the load script's:

```bash
docker compose stop simulator
```

Wait about 70 s for its traffic to leave the 1-minute window. Then run a baseline: 5 requests/s for 150 s, IDs `load-baseline-NNNNN`, standard-library Python only:

```bash
python3 scripts/load.py --stage baseline --rate 5 --duration 150 --out /tmp/baseline.json
```

Compute the server-side figures (percentiles, Apdex, faults, log counts) for exactly that stage's window:

```bash
python3 scripts/e1_stage_stats.py baseline $(jq -r .start_utc /tmp/baseline.json) $(jq -r .end_utc /tmp/baseline.json)
```

Switch on the fault used in the report, 500 ms on every 5th reading:

```bash
curl -s -XPUT localhost:8000/admin/faults -H 'content-type: application/json' -d '{"delay_ms":500,"delay_every_n":5,"error_every_n":0}'
```

Run the same load with `--stage fault` and compute its figures in the same way. Then switch the fault off. This is the undo step, and a restart also resets it:

```bash
curl -s -XDELETE localhost:8000/admin/faults
```

Run the same load once more with `--stage recovery`. Finally, restart the simulator:

```bash
docker compose start simulator
```

The report's own raw results are in `docs/results/`.

**E2: cardinality explosion.** This runs a throwaway exporter with its own metrics registry, capped at 100 unique IDs. It starts the exporter with a `request_id` label, then without it, and prints the series counts. It always cleans up afterwards (the exporter is stopped, and the Prometheus targets file goes back to `[]`):

```bash
scripts/cardinality_demo.sh /tmp/e2.json
```

Check that nothing is left behind. This should print `[]`, and the service shouldn't be listed:

```bash
cat monitoring/prometheus/file_sd/cardinality-demo.json; docker compose --profile e2 ps -a
```

The demo's series stay in Prometheus's history until retention removes them (7 days). To remove them immediately, `docker compose down -v` deletes all metrics history.

### Run the device simulator

`docker compose up -d --build` starts the API and the simulator together. The simulator runs 20 devices, each sending one reading every 5 s. The last four misbehave on purpose:

| Device | Behaviour |
|---|---|
| dev-017 | overheats |
| dev-018 | battery drains |
| dev-019 | sends invalid data |
| dev-020 | drops offline for 60 s at a time |

To follow the simulator's output, with a summary every 30 s:

```bash
docker compose logs -f simulator --no-log-prefix
```

To run the simulator locally against a running API, faster and for 30 s only:

```bash
.venv/bin/python -m simulator.simulate --interval 1 --duration 30
```

The options are `--api-url` (default `http://localhost:8000`), `--devices` (20), `--interval` in seconds per device (5), `--seed` (42) and `--duration` in seconds, where 0 means run forever. They can also be set with the environment variables `API_URL`, `SIM_DEVICES`, `SIM_INTERVAL_S`, `SIM_SEED` and `SIM_DURATION_S`.

### Send a reading

The device timestamp must include a timezone and can't be more than 60 s in the future.

```bash
curl -s -XPOST localhost:8000/readings -H 'content-type: application/json' -d "{\"device_id\":\"dev-001\",\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"temperature_c\":22.5,\"humidity_pct\":45.0,\"battery_pct\":88.0}"
```

- A normal reading returns `201` with `"is_anomaly": false`.
- A value outside the expected range (for example `"temperature_c": 40`) returns `201` with `"is_anomaly": true` and `"anomaly_reasons": ["temperature_high"]`.
- Physically impossible or malformed data (for example `"humidity_pct": 150`, a missing field, or a `device_id` that isn't `dev-NNN`) returns `422` and isn't stored.

### Query devices and anomalies

List all devices with their status (`ok`, `anomaly`, or `stale` after 30 s of silence):

```bash
curl -s localhost:8000/devices | jq
```

List only the devices with a given status:

```bash
curl -s 'localhost:8000/devices?status=anomaly' | jq
```

Show one device, including its latest reading and counters:

```bash
curl -s localhost:8000/devices/dev-001 | jq
```

Show a device's history, newest first. `limit` can be 1–500, and `anomalies_only` can be added:

```bash
curl -s 'localhost:8000/devices/dev-001/readings?limit=10' | jq
```

Show the latest anomalies across the whole fleet:

```bash
curl -s 'localhost:8000/anomalies?limit=10' | jq
```

### Inject and undo a fault

Fault injection is used for the experiments. It affects only `POST /readings`, is off by default, and is reset by a restart.

Add 500 ms of latency to every 5th reading:

```bash
curl -s -XPUT localhost:8000/admin/faults -H 'content-type: application/json' -d '{"delay_ms":500,"delay_every_n":5,"error_every_n":0}'
```

Make every 10th reading fail with a 500:

```bash
curl -s -XPUT localhost:8000/admin/faults -H 'content-type: application/json' -d '{"delay_ms":0,"delay_every_n":1,"error_every_n":10}'
```

Check the current setting:

```bash
curl -s localhost:8000/admin/faults
```

Undo the fault, turning everything off:

```bash
curl -s -XDELETE localhost:8000/admin/faults
```

### View logs

The API writes one JSON object per line. To follow the logs:

```bash
docker compose logs -f api --no-log-prefix
```

To show only the access-log lines:

```bash
docker compose logs api --no-log-prefix | jq -c 'select(.event=="http_request")'
```

Every response carries an `X-Request-ID` header. You can send your own with `-H 'X-Request-ID: my-id'`, and it appears as `request_id` on every log line for that request.

### Run without Docker

To run the API without Docker:

```bash
.venv/bin/python -m app
```

## Test

### Automated tests

The unit and API tests use a temporary SQLite file and don't need Docker.

```bash
python3.12 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements-dev.txt
```

```bash
.venv/bin/pytest -q
```

### Manual check (about 5 minutes)

Run these steps after `docker compose up -d --build`. Each step says what you should see.

1. **Health.** `curl -s localhost:8000/health` returns `{"status":"ok","db":"ok"}`, and `docker compose ps` shows `api` as `(healthy)`.
2. **Dashboard loads.** Open http://localhost:8000. The top bar says **API healthy**, and the table fills with devices dev-001 to dev-020.
3. **Validation, from the dashboard's "Send a test reading" panel:**
   - **Normal** gives *201 Stored as normal*.
   - **Too hot** gives *201 Stored as anomaly: temperature_high*.
   - **Impossible humidity** gives *422 Rejected, not stored* with `humidity_pct: Input should be less than or equal to 100`.
4. **The faulty devices show up.** Wait about 3 minutes after startup:
   - dev-017 (overheat) and dev-018 (low battery) show **anomaly**, and they appear in *Recent anomalies* as `temperature_high` and `battery_low`.
   - dev-020 turns **stale** for about 30 s of every 2 minutes. The **Stale** filter chip shows it.
   - Click dev-017. Its temperature chart climbs, with dots once it passes 35 °C.
5. **The invalid device is rejected.** The simulator's summary shows `status_422 > 0` (from dev-019) and `status_5xx == 0`:
   ```bash
   docker compose logs simulator --no-log-prefix | jq -c 'select(.event=="sim_summary") | .stats' | tail -1
   ```
6. **Logs are structured.** This prints `OK` only if every log line is valid JSON:
   ```bash
   docker compose logs api --no-log-prefix | jq -c . > /dev/null && echo OK
   ```
   Searching for a request ID shown by the dashboard (`ui-…`) returns that request's log lines:
   ```bash
   docker compose logs api --no-log-prefix | jq -c 'select(.request_id=="ui-XXXXXXXXXXXX")'
   ```
7. **Fault injection.** In the dashboard's *Fault injection* panel, set Delay to 500 and click **Apply**. The yellow banner appears, and the logged `duration_ms` for `/readings` rises to about 500:
   ```bash
   docker compose logs api --no-log-prefix --since 15s | jq -r 'select(.path=="/readings") | .duration_ms'
   ```
   Click **Turn off** in the banner. The banner disappears and the latency drops back to a few ms.
8. **Data persists.** Note dev-001's reading count, run `docker compose restart api`, and check again. The count keeps growing from where it was instead of starting from zero.
9. **Logs reach Kibana.** Send a reading with your own request ID (add `-H 'X-Request-ID: <your id>'` to the curl in [Send a reading](#send-a-reading)), then search `request_id : "<your id>"` in Kibana. It shows the reading's 2–3 log lines within a few seconds.
10. **Logs outlive the container.** Run `docker compose up -d --force-recreate api`. `docker compose logs api | grep <your id>` now finds nothing, but the Kibana search still finds every line.
11. **Prometheus scrapes all targets.** This lists `node up`, `prometheus up`, `sensor-api up` and `sensor-grpc up`:
   ```bash
   curl -s localhost:9090/api/v1/targets | jq -r '.data.activeTargets[] | "\(.labels.job) \(.health)"'
   ```
12. **Grafana dashboards have data.** Open http://localhost:3000/d/sensor-service. After about a minute (the rate window), *Stored readings / s* shows about 3.9, *p95 latency* about 20 ms, and *Devices by status* matches the API. Then open http://localhost:3000/d/node-host. *Hostname* shows the machine running Docker, and the CPU, memory, disk and network panels have data.

## Clean up

Stop the containers. Stored data is kept in the volumes: `sensor-data` (readings), `prometheus-data` (metrics history), `grafana-data` (Grafana's internal state), `es-data` (the indexed logs) and `filebeat-data` (Filebeat's read positions):

```bash
docker compose down
```

Stop the containers **and delete all stored data**: readings, metrics history, Grafana state and all logs. The dashboards themselves are files in `monitoring/grafana/dashboards/`, so they come back on the next start. This can't be undone:

```bash
docker compose down -v
```

Remove the built image:

```bash
docker image rm sensor-api:dev
```

Remove the downloaded monitoring images:

```bash
docker compose --profile e2 rm -sf cardinality-demo   # only if the E2 demo was interrupted
docker image rm prom/prometheus:v3.15.0 prom/node-exporter:v1.12.1 grafana/grafana:13.2.2 elasticsearch:9.5.3 kibana:9.5.3 elastic/filebeat:9.5.3 curlimages/curl:8.16.0
```
