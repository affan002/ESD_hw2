# Build & verification log

The stage-by-stage evidence behind [REPORT.md](../REPORT.md): what was built at each stage, and the exact command and real output that proved it worked. The report summarises these results; this file keeps the raw evidence.

- [Part A: the app](#part-a-the-app) (stages 0–9)
- [Part B: metrics](#part-b-metrics) (stages B0–B9)
- [Part C: logs](#part-c-logs) (stages C0–C9)
- [Part D: system design](#part-d-system-design) (stages D1–D3)
- [Part E: experiments](#part-e-experiments) (E1.1–E1.6, E2.1–E2.4)

---

## Part A: the app

Each entry records what was built and the command and output that proved it worked.

#### Stage 0 — Scaffold

**Built:**
- A FastAPI app factory (`app/main.py`) with a `GET /health` endpoint.
- Settings read from environment variables (`app/config.py`).
- The `python -m app` entry point, which runs a single uvicorn worker.
- A Dockerfile (`python:3.12-slim`, non-root user) and a `docker-compose.yml` with the `api` service and the `sensor-data` volume.
- A pytest setup.

**Verified:**
```
$ .venv/bin/pytest -q
1 passed in 0.07s

$ docker compose up -d --build api && curl -s localhost:8000/health
{"status":"ok"}

$ docker compose ps --format '{{.Service}} {{.Status}}'
api Up 15 seconds (healthy)
```

#### Stage 1 — Structured logging and request IDs

**Built:**
- `app/logging_setup.py`: a JSON formatter that writes one object per line to stdout. Every line has the fields `timestamp` (UTC, ms, `Z`), `level`, `service`, `logger`, `event`, `message` and `request_id`. Extra fields are only emitted if they're on a fixed whitelist, so request bodies or secrets can't end up in logs by accident.
- `app/middleware.py`: a pure ASGI middleware that does three things:
  - Accepts a valid incoming `X-Request-ID` or generates a UUID4, and echoes it on every response.
  - Writes one `http_request` log line per request with `method`, the route *template* as `path`, `status_code` and `duration_ms`.
  - Turns crashes into a JSON 500 that keeps the request ID, and logs them as `unhandled_exception`.
- Uvicorn runs with `log_config=None` and its access log turned off, so its own messages come out in the same JSON format.

**Verified:**
```
$ .venv/bin/pytest -q
8 passed in 0.15s

$ curl -si -H 'X-Request-ID: test-123' localhost:8000/health | grep -i x-request-id
x-request-id: test-123

$ docker compose logs api --no-log-prefix | jq -c . >/dev/null && echo OK     # every line is valid JSON
OK

$ docker compose logs api --no-log-prefix | jq -c 'select(.event=="http_request")' | tail -2
{"timestamp":"2026-09-25T20:35:04.326Z","level":"INFO","service":"sensor-api","logger":"app.http","event":"http_request","message":"GET /health -> 200","request_id":"test-123","method":"GET","path":"/health","status_code":200,"duration_ms":1.31}
{"timestamp":"2026-09-25T20:35:04.348Z","level":"INFO","service":"sensor-api","logger":"app.http","event":"http_request","message":"GET unmatched -> 404","request_id":"1eb0e5673c15489ea0b18dcca4d34671","method":"GET","path":"unmatched","status_code":404,"duration_ms":0.43}
```

#### Stage 2 — Data model and storage

**Built:**
- `app/db.py`: SQLite (stdlib `sqlite3`, WAL journal, `busy_timeout=5000`) with two tables:
  - `readings`: one row per accepted reading, with both the device timestamp (`recorded_at`) and the server timestamp (`received_at`).
  - `devices`: one row per device, updated on every reading (`first_seen`, `last_seen`, `reading_count`, `anomaly_count`, `last_reading_id`). Devices register themselves automatically on their first reading.

  Inserting a reading and updating its device happen in one `BEGIN IMMEDIATE` transaction. Each request gets its own connection.
- `app/models.py`: Pydantic models. `ReadingIn` enforces the physical sensor limits; the output models are `ReadingOut`, `DeviceStatus` and the list wrappers.
- `/health` now checks the database with `SELECT 1`. It returns `503 {"status":"degraded","db":"error"}` if the database is unreachable.

**Why SQLite:** a single-file database on a Docker named volume. It needs no extra service, survives container restarts and easily handles the ~4 writes/second the simulator produces.

**Verified:**
```
$ .venv/bin/pytest -q
12 passed in 0.27s

$ curl -s localhost:8000/health
{"status":"ok","db":"ok"}

$ docker compose exec api python -c "import sqlite3;print(sqlite3.connect('/data/sensors.db').execute(\"select name from sqlite_master where type='table'\").fetchall())"
[('devices',), ('readings',), ('sqlite_sequence',)]

$ docker compose exec api ls -l /data
-rw-r--r-- 1 app app 28672 Sep 25 20:36 sensors.db
```

#### Stage 3 — Anomaly rule

**Built:** `app/anomaly.py`, which contains two pure functions:
- `evaluate()` returns a list of reason codes in a fixed order.
- `compute_status()` works out each device's status.

The service separates **impossible** data from **concerning** data:

| Field | Physical range (else `422`, not stored) | Expected range, inclusive (else stored as anomaly) | Reason codes |
|---|---|---|---|
| `temperature_c` | −40 … 85 °C | 10.0 … 35.0 | `temperature_low`, `temperature_high` |
| `humidity_pct` | 0 … 100 %RH | 20.0 … 80.0 | `humidity_low`, `humidity_high` |
| `battery_pct` | 0 … 100 % | ≥ 15.0 | `battery_low` |

A reading is an anomaly if it has at least one reason code. Device status is checked in this order:
1. `stale`: no reading for more than `STALE_AFTER_S` (30 s by default), based on server receive time.
2. `anomaly`: the latest reading is anomalous.
3. `ok`: everything else.

**Verified:** boundary tests confirm that 10.0 and 35.0 °C are normal while 9.99 and 35.01 are not, and likewise for the humidity and battery bounds. Other tests cover several reasons at once and the status order.
```
$ .venv/bin/pytest -q tests/test_anomaly.py
16 passed in 0.03s
```

#### Stage 4 — Ingestion endpoint `POST /readings`

**Built:** `POST /readings` handles each reading in this order:
1. Validates it against the physical limits. The device timestamp must include a timezone and be no more than 60 s in the future.
2. Normalises timestamps to UTC.
3. Runs `evaluate()`.
4. Stores the reading and updates the device in one transaction.
5. Returns `201` with the stored reading.

Log events: `device_registered` (a device's first reading), `reading_stored` (every accepted reading) and `anomaly_detected` (WARNING, with reasons and values).

A custom `RequestValidationError` handler logs `reading_rejected` (WARNING) with only the failing field and error type, never the submitted values. It then returns FastAPI's standard `422` body.

**Verified:**
```
$ .venv/bin/pytest -q
41 passed in 0.67s

$ curl -s -XPOST localhost:8000/readings -H 'content-type: application/json' \
    -d '{"device_id":"dev-001","timestamp":"2026-09-25T20:39:43Z","temperature_c":22.5,"humidity_pct":45.0,"battery_pct":88.0}'
{"id":1,"device_id":"dev-001","timestamp":"2026-09-25T20:39:43.000Z","received_at":"2026-09-25T20:39:43.421Z","temperature_c":22.5,"humidity_pct":45.0,"battery_pct":88.0,"is_anomaly":false,"anomaly_reasons":[]}   [201]

$ curl ... -d '{"device_id":"dev-002", ..., "temperature_c":40.0, ...}'
{"id":2,"device_id":"dev-002",...,"is_anomaly":true,"anomaly_reasons":["temperature_high"]}   [201]

$ curl ... -d '{"device_id":"dev-003", ..., "humidity_pct":150, ...}'
{"detail":[{"type":"less_than_equal","loc":["body","humidity_pct"],"msg":"Input should be less than or equal to 100","input":150,"ctx":{"le":100.0}}]}   [422]

$ docker compose logs api --no-log-prefix | jq -c 'select(.event|test("anomaly|rejected"))'
{"timestamp":"2026-09-25T20:39:43.437Z","level":"WARNING","service":"sensor-api","logger":"app","event":"anomaly_detected","message":"anomaly detected: temperature_high","request_id":"9314e35473544746958428d4c0bcd101","device_id":"dev-002","reading_id":2,"anomaly_reasons":["temperature_high"],"temperature_c":40.0,"humidity_pct":45.0,"battery_pct":88.0}
{"timestamp":"2026-09-25T20:39:43.446Z","level":"WARNING","service":"sensor-api","logger":"app","event":"reading_rejected","message":"reading rejected","request_id":"3df52705abb74515a4e3a78d8bedfaaf","device_id":"dev-003","errors":[{"field":"humidity_pct","type":"less_than_equal"}]}
```

#### Stage 5 — Query endpoints

**Built:**

| Route | Returns |
|---|---|
| `GET /devices?status=ok\|anomaly\|stale` | `{"count", "devices":[DeviceStatus]}`, sorted by `device_id`, optionally filtered by status |
| `GET /devices/{device_id}` | one `DeviceStatus`: `status`, `first_seen`, `last_seen`, `reading_count`, `anomaly_count` and `latest_reading`; `404` if the device is unknown |
| `GET /devices/{device_id}/readings?limit=1..500&anomalies_only=` | that device's history, newest first |
| `GET /anomalies?limit=1..500` | the most recent anomalous readings across the whole fleet, newest first |

Status is computed at query time with `compute_status()`, so a device becomes `stale` just by staying silent. No background job is needed.

**Verified:** the three devices below show all three statuses. dev-002 last reported in Stage 4, more than 30 s earlier. dev-001's data from Stage 4 survived the container rebuild, which confirms the volume persists.
```
$ .venv/bin/pytest -q
51 passed in 1.24s

$ curl -s localhost:8000/devices | jq -c '.count, [.devices[]|{device_id,status,reading_count}]'
3
[{"device_id":"dev-001","status":"ok","reading_count":3},{"device_id":"dev-002","status":"stale","reading_count":1},{"device_id":"dev-004","status":"anomaly","reading_count":1}]

$ curl -s 'localhost:8000/devices/dev-001/readings?limit=2' | jq '.readings|length'
2

$ curl -s 'localhost:8000/anomalies?limit=5' | jq -c '[.readings[]|{id,device_id,anomaly_reasons}]'
[{"id":5,"device_id":"dev-004","anomaly_reasons":["battery_low"]},{"id":2,"device_id":"dev-002","anomaly_reasons":["temperature_high"]}]

$ curl -s -w ' [%{http_code}]\n' localhost:8000/devices/dev-999
{"detail":"device not found"} [404]

$ curl -s 'localhost:8000/devices?status=stale' | jq -c '[.devices[].device_id]'
["dev-002"]
```

#### Stage 6 — Fault-injection hook (for Part E)

**Built:** `app/faults.py` and three operator routes: `GET`, `PUT` and `DELETE /admin/faults`. The config is `{"delay_ms": 0..5000, "delay_every_n": ≥1, "error_every_n": ≥0}`.
- The fault applies only to `POST /readings`, and only after the body has passed validation.
- It's deterministic: it hits every Nth request, so an experiment can be repeated exactly.
- The delay uses `time.sleep` inside the sync handler's threadpool thread, so only the affected request slows down.
- An injected error returns `500 {"detail":"injected fault"}` before anything is stored.
- The state is in memory and off by default. `DELETE` or a restart undoes it.
- Every change is logged as `fault_config_changed`, and every injected fault as `fault_injected` (`fault=delay|error`).
- The admin routes have no authentication. They're meant only for local experiments, and a real deployment would put them behind auth or remove them.

**Verified:**
```
$ .venv/bin/pytest -q
57 passed in 2.13s

baseline POST                                   → 201 0.019539s
$ curl -s -XPUT localhost:8000/admin/faults -H 'content-type: application/json' -d '{"delay_ms":500,"delay_every_n":1,"error_every_n":0}'
{"delay_ms":500,"delay_every_n":1,"error_every_n":0}
POST, POST                                      → 201 0.522141s, 201 0.523538s
$ curl -s -XPUT ... -d '{"delay_ms":0,"delay_every_n":1,"error_every_n":2}'
POST, POST                                      → 201 0.006577s, 500 0.003602s
$ curl -s -XDELETE localhost:8000/admin/faults
{"delay_ms":0,"delay_every_n":1,"error_every_n":0}
POST, POST                                      → 201 0.006148s, 201 0.004985s

$ docker compose logs api --no-log-prefix | jq -c 'select(.event|test("fault"))'   (trimmed)
{"level":"WARNING","event":"fault_config_changed","config":{"delay_ms":500,"delay_every_n":1,"error_every_n":0},...}
{"level":"WARNING","event":"fault_injected","fault":"delay","delay_ms":500,"request_id":"cf11b6d8264f409ab3d7d95f1bf2f7ad",...}
{"level":"ERROR","event":"fault_injected","fault":"error","request_id":"02fcbc9e53624dcda8762513eeaa7bdc",...}
```

#### Stage 7 — Device simulator

**Built:** `simulator/simulate.py`, a single-process fleet simulator that runs in the same image as the compose service `simulator`.
- Defaults: 20 devices (`dev-001`…`dev-020`), one reading per device every 5 s, spread evenly. That's about 4 requests/second in a fixed round-robin order.
- Each device's randomness is seeded with `seed + index`, so a run can be repeated exactly.
- Every request carries `X-Request-ID: sim-<12 hex>`, and the simulator logs the same ID. One reading can therefore be traced across both services' logs.
- It writes a `sim_summary` log line every 30 s and on exit.

| Devices | Profile | Behaviour | Effect on the API |
|---|---|---|---|
| dev-001…016 | normal | temp = base U(19,25) + 1.5·sin(2πt/600) + N(0,0.3); humidity = base U(35,60) + N(0,1); battery U(60,100) drains 0.002/reading | `201`, `ok` |
| dev-017 | overheat | temp climbs 0.6 °C per reading from base, resets once above 45 °C | `201` with `temperature_high` after ~17–27 readings; cycles |
| dev-018 | low_battery | battery starts at 20 %, drains 0.25/reading, floor 1 % | `201` with `battery_low` from reading ~21 |
| dev-019 | invalid | 30 % of sends are invalid, rotating through humidity 150 → missing `temperature_c` → timestamp +1 h | `422` (not stored) |
| dev-020 | dropout | online for 60 s, silent for 60 s, repeating | `stale` for ~30 s of every 2 minutes |

**Verified:** a 30 s local run against the Docker API, with `--interval 1`, which is 5× faster than the default.
```
$ .venv/bin/pytest -q
64 passed in 2.35s

$ .venv/bin/python -m simulator.simulate --interval 1 --duration 30 > sim.log; echo "exit=$?"
exit=0
$ jq -c 'select(.event=="sim_summary")|.stats' sim.log | tail -1
{"sent":600,"status_201":591,"status_422":9,"status_5xx":0,"status_other":0,"conn_errors":0}
$ jq -c 'select(.event=="sim_send_failed")|{device_id,status_code,fault,request_id}' sim.log | head -3
{"device_id":"dev-019","status_code":422,"fault":"humidity_out_of_range","request_id":"sim-49d02c2010c2"}
{"device_id":"dev-019","status_code":422,"fault":"missing_temperature","request_id":"sim-18b7042d2c40"}
{"device_id":"dev-019","status_code":422,"fault":"future_timestamp","request_id":"sim-e7f1ee29eede"}

$ curl -s localhost:8000/devices | jq -c '.count, [.devices[]|select(.device_id>="dev-017")|{device_id,status,anomaly_count}]'
20
[{"device_id":"dev-017","status":"anomaly","anomaly_count":9},{"device_id":"dev-018","status":"anomaly","anomaly_count":10},{"device_id":"dev-019","status":"ok","anomaly_count":0},{"device_id":"dev-020","status":"ok","anomaly_count":0}]

# the same request ID, found in the API's logs
$ docker compose logs api --no-log-prefix | jq -c 'select(.request_id=="sim-49d02c2010c2")|{service,event,path,status_code,errors}'
{"service":"sensor-api","event":"reading_rejected","path":null,"status_code":null,"errors":[{"field":"humidity_pct","type":"less_than_equal"}]}
{"service":"sensor-api","event":"http_request","path":"/readings","status_code":422,"errors":null}
```

#### Stage 8 — End-to-end demo in Docker

**Built:** no new features. The full stack was run from a clean state with default settings (20 devices, a reading every 5 s).

**Verified:** stack started at 20:44:54 UTC and was checked at 20:47:57 UTC.
```
$ docker compose down -v && docker compose up -d --build
$ docker compose ps --format '{{.Service}} {{.Status}}'
api Up 13 seconds (healthy)
simulator Up 8 seconds

# 1. all devices registered; the faulty ones are flagged
$ curl -s localhost:8000/devices | jq -c '.count, [.devices[]|select(.device_id>="dev-017")|{device_id,status,reading_count,anomaly_count}]'
20
[{"device_id":"dev-017","status":"anomaly","reading_count":38,"anomaly_count":17},{"device_id":"dev-018","status":"anomaly","reading_count":38,"anomaly_count":18},{"device_id":"dev-019","status":"ok","reading_count":26,"anomaly_count":0},{"device_id":"dev-020","status":"ok","reading_count":24,"anomaly_count":0}]
$ curl -s localhost:8000/devices | jq -c '[.devices[].status]|group_by(.)|map({(.[0]):length})|add'
{"anomaly":2,"ok":18}

# dev-020 goes stale ~30 s after its dropout starts
20:48:14 dev-020 ok    last_seen=2026-09-25T20:47:46.618Z
20:48:17 dev-020 stale last_seen=2026-09-25T20:47:46.618Z
$ curl -s 'localhost:8000/devices?status=stale' | jq -c '[.devices[].device_id]'
["dev-020"]

# 2. fleet anomalies
$ curl -s 'localhost:8000/anomalies?limit=5' | jq -c '[.readings[]|{device_id,anomaly_reasons}]'
[{"device_id":"dev-018","anomaly_reasons":["battery_low"]},{"device_id":"dev-017","anomaly_reasons":["temperature_high"]},{"device_id":"dev-018","anomaly_reasons":["battery_low"]},...]

# 3. the simulator's view: invalid readings rejected, no server errors
$ docker compose logs simulator --no-log-prefix | jq -c 'select(.event=="sim_summary")|.stats' | tail -1
{"sent":713,"status_201":701,"status_422":12,"status_5xx":0,"status_other":0,"conn_errors":0}

# 4. restart: data persists, and the simulator survives the gap
$ docker compose restart api
dev-001 reading_count: before=43, after=44   (counts continued, not reset)
$ docker compose logs simulator --no-log-prefix | jq -c 'select(.event=="sim_send_failed" and .fault==null)|{error,status_code}' | sort | uniq -c
      5 {"error":"ConnectError","status_code":null}

# 5. test suite
$ .venv/bin/pytest -q
64 passed in 2.33s

# 6. clean-up
$ docker compose down        → containers: 0, volume esd_hw1_sensor-data still present
$ docker compose down -v     → Volume esd_hw1_sensor-data Removed
```

#### Stage 9 — Operator dashboard

**Built:**
- `app/static/index.html`, `styles.css` and `app.js`: plain HTML, CSS and JavaScript with no framework and no build step. `GET /` serves the page and `/static` serves the assets, from the same container. The page supports light and dark mode and adapts down to phone width.
- The page shows summary cards, a device table you can filter, a device detail panel with SVG charts of the last 50 readings, a feed of recent anomalies, a form for sending test readings, and fault-injection controls with a warning banner.

Design rules:
- The page calls only the public API, and adds no UI-specific endpoints.
- It never duplicates the thresholds: highlights and chart dots come from `anomaly_reasons`.
- All server data is inserted with `textContent`, so it can't inject HTML.
- Every UI request sends `X-Request-ID: ui-<12 hex>`.
- The access-log middleware now also labels requests to mounted apps with their mount prefix (`/static`) instead of `unmatched`, so the `path` field stays a small, fixed set of values.

**Verified** in a browser against the Docker stack, and with tests:
```
$ .venv/bin/pytest -q
69 passed

GET /                      → 200 text/html, no console errors
Click dev-017              → detail panel: 9 readings, temperature chart climbing 22.5 → 27.9 °C
Preset "Too hot" + Send    → "201 Stored as anomaly (reading #468): temperature_high"  request_id: ui-59e21fbc176a
Preset "Impossible humidity" + Send
                           → "422 Rejected, not stored · humidity_pct: Input should be less than or equal to 100"
Fault form: delay 300 ms   → banner "Fault injection active on POST /readings: 300 ms delay on every request."
                             API log: /readings duration_ms 319.26 320.12 318.76 320.32 320.42
Banner "Turn off"          → banner hidden, form reset to 0, "All faults off"
375 px viewport            → page scrollWidth 375 (no horizontal scroll); cards 2 per row; panels stacked

# the UI's traffic can be told apart in the logs by request ID prefix (last 60 s)
$ docker compose logs api --no-log-prefix --since 60s | jq -r 'select(.event=="http_request")|.request_id[0:3]' | sort | uniq -c
    208 sim
     53 ui-
     16 <uuid>   (Docker healthchecks, no header)
```

Bugs found and fixed during this check:
- The battery bar fill didn't show, because an inline `<span>` ignores its width.
- `replaceChildren(null)` rendered the literal text "null".
- A 0.02 % battery change was stretched to the full height of its chart. Charts now keep a minimum 2-unit vertical range.
- The table needed a horizontal scrollbar at 1024 px. The layout now stacks below 1180 px.

---

## Part B: metrics

#### Stage B0 — `prometheus_client` and `/metrics`

**Built:**
- `prometheus_client==0.26.0` added to `requirements.txt`.
- `app/metrics.py`, with a `render()` function.
- `GET /metrics`, kept out of the OpenAPI docs.

**Verified:**
```
$ .venv/bin/pytest -q
71 passed in 2.51s

$ curl -s localhost:8000/metrics | grep -E '^(process_resident_memory_bytes|process_cpu_seconds_total|process_open_fds|python_info)'
python_info{implementation="CPython",major="3",minor="12",patchlevel="14",version="3.12.14"} 1.0
process_resident_memory_bytes 5.7769984e+07
process_cpu_seconds_total 1.24
process_open_fds 16.0
```

#### Stage B1 — HTTP metrics

**Built:** three metrics, recorded in the existing request middleware (`app/middleware.py`), so every route is covered without touching any handler:

| Metric | Type | Labels |
|---|---|---|
| `sensor_http_requests_total` | Counter | `method`, `route`, `status_code` |
| `sensor_http_request_duration_seconds` | Histogram | `method`, `route` |
| `sensor_http_requests_in_progress` | Gauge | none |

- The histogram buckets are 5 ms, 10, 25, 50, 100, 250, 500 ms, 1, 2.5 and 5 s.
- The gauge is incremented when a request arrives and decremented when it finishes.
- `route` is the same bounded route template the access log already uses: `/devices/{device_id}`, `/static`, or `unmatched`. It's never the raw URL, so `GET /devices/dev-017` and `GET /devices/dev-018` share one series.

**Verified:**
```
$ .venv/bin/pytest -q
75 passed in 2.82s

$ curl -s localhost:8000/metrics | grep '^sensor_http_requests_total'
sensor_http_requests_total{method="POST",route="/readings",status_code="201"} 32.0
sensor_http_requests_total{method="POST",route="/readings",status_code="422"} 1.0
sensor_http_requests_total{method="GET",route="/devices/{device_id}",status_code="200"} 1.0
sensor_http_requests_total{method="GET",route="/health",status_code="200"} 3.0
...

$ curl -s localhost:8000/metrics | grep 'duration_seconds_bucket' | grep 'POST' | grep 'readings"'     (cumulative)
..._bucket{le="0.005",method="POST",route="/readings"} 1.0
..._bucket{le="0.01",method="POST",route="/readings"} 11.0
..._bucket{le="0.025",method="POST",route="/readings"} 67.0
..._bucket{le="0.1",method="POST",route="/readings"} 68.0
..._bucket{le="+Inf",method="POST",route="/readings"} 68.0

$ curl -s localhost:8000/metrics | grep '^sensor_http_requests_in_progress'
sensor_http_requests_in_progress 1.0        ← the /metrics request itself
```

#### Stage B2 — Business metrics

**Built:** metrics for the app's real events, recorded where each event happens:

| Metric | Type | Labels | Recorded in |
|---|---|---|---|
| `sensor_readings_total` | Counter | `outcome` = `normal` \| `anomaly` \| `rejected` \| `failed` | `POST /readings` handler (stored or failed) and the validation handler (rejected) |
| `sensor_reading_validation_errors_total` | Counter | `field`, `reason` (the Pydantic error type) | validation handler, once per failing field |
| `sensor_anomalies_total` | Counter | `reason` = one of the 5 anomaly codes | `POST /readings`, once per reason |
| `sensor_db_write_duration_seconds` | **Summary** | none | wraps `db.insert_reading()` |
| `sensor_faults_injected_total` | Counter | `fault` = `delay` \| `error` | `FaultState.apply()` |
| `sensor_fault_active` | Gauge | `fault` | set on every change to the fault config |
| `sensor_devices` | Gauge | `status` = `ok` \| `anomaly` \| `stale` | a custom collector that runs at scrape time |

Design notes:
- **Readings get one counter with an `outcome` label**, not one counter per outcome. The total, the share stored and the share rejected all come from a single metric, and the four outcomes add up to every `POST /readings` attempt.
- **`sensor_devices` is computed during each scrape, not updated on each event.** A device becomes stale by *not* sending anything, so there's no event to count. The collector runs the same `compute_status()` rule that `GET /devices` uses, so the metric and the API can't disagree.
- **Label values are bounded.** `field` is limited to the 5 known fields plus `body` (a malformed JSON body reports a character position as its location, which could be anything). `reason` values come from Pydantic's fixed list of error types.
- Every known label combination is created at 0 at startup, so `rate()` and the dashboard panels have data before the first event.
- The Dockerfile sets `PROMETHEUS_DISABLE_CREATED_SERIES=True`, which removes the library's extra `*_created` series.

**Verified:** after about 40 s of simulator traffic.
```
$ .venv/bin/pytest -q
83 passed in 3.29s

$ curl -s localhost:8000/metrics | grep -E '^(sensor_readings_total|sensor_anomalies_total|sensor_reading_validation_errors_total|sensor_db_write|sensor_devices|sensor_fault)'
sensor_readings_total{outcome="normal"} 140.0
sensor_readings_total{outcome="anomaly"} 16.0
sensor_readings_total{outcome="rejected"} 2.0
sensor_readings_total{outcome="failed"} 0.0
sensor_reading_validation_errors_total{field="humidity_pct",reason="less_than_equal"} 1.0
sensor_reading_validation_errors_total{field="temperature_c",reason="missing"} 1.0
sensor_anomalies_total{reason="temperature_high"} 8.0          ← dev-017 overheating
sensor_anomalies_total{reason="battery_low"} 8.0               ← dev-018 battery
sensor_db_write_duration_seconds_count 156.0                    ← = 140 normal + 16 anomaly (every stored reading)
sensor_db_write_duration_seconds_sum 1.4799524540030689         ← average ≈ 9.5 ms per write
sensor_fault_active{fault="delay"} 0.0
sensor_devices{status="ok"} 18.0
sensor_devices{status="anomaly"} 2.0
sensor_devices{status="stale"} 1.0

$ curl -s localhost:8000/devices | jq -c '[.devices[].status]|group_by(.)|map({(.[0]):length})|add'
{"anomaly":2,"ok":18,"stale":1}                                 ← same as the gauge
```

#### Stage B3 — Prometheus

**Built:**
- `monitoring/prometheus/prometheus.yml` (scrape every 5 s; jobs `sensor-api` and `prometheus`).
- A `prometheus` service in `docker-compose.yml`: port 9090, volume `prometheus-data`, 7-day retention.

**Verified:**
```
$ curl -s localhost:9090/-/ready
Prometheus Server is Ready.

$ curl -s localhost:9090/api/v1/targets | jq -r '.data.activeTargets[] | "\(.labels.job) \(.scrapeUrl) \(.health)"'
prometheus  http://localhost:9090/metrics  up
sensor-api  http://api:8000/metrics        up

# PromQL through the HTTP API (the same queries work at http://localhost:9090/query)
sum by (outcome) (rate(sensor_readings_total[1m]))
  normal 2.83/s   anomaly 0.197/s   rejected 0.036/s   failed 0
sum by (route) (rate(sensor_http_requests_total[1m]))
  /readings 3.06/s   /health 0.32/s   /metrics 0.18/s   /devices 0.16/s (the open dashboard, polling every 5 s) ...
histogram_quantile(0.95, sum by (le) (rate(sensor_http_request_duration_seconds_bucket{route="/readings"}[1m])))
  0.0246   (p95 ≈ 25 ms)
histogram_quantile(0.99, ...same...)
  0.0414   (p99 ≈ 41 ms)
rate(sensor_db_write_duration_seconds_sum[1m]) / rate(sensor_db_write_duration_seconds_count[1m])
  0.0083   (average DB write ≈ 8.3 ms)
count({job="sensor-api"})
  156      (series exposed by the app)
```

**Something I learned here:** straight after Prometheus started, `sum(rate(sensor_readings_total[1m]))` reported only **0.56/s**, then 1.83/s, then about 3/s, even though the simulator's own log showed a steady 3.9/s. `rate()` divides the increase by the *whole* 60 s window, so until the window holds a full minute of samples it underestimates. The same thing happens after an app restart, when the counter resets to 0. That's why each experiment phase needs to run for longer than the rate window.

#### Stage B4 — Node Exporter

**Built:**
- A `node-exporter` service using the host network, the host PID namespace and a read-only mount of `/`.
- Prometheus job `node` → `host.docker.internal:9100`, plus `extra_hosts: host-gateway` on the `prometheus` service.

**Verified:** see the comparison table in [REPORT.md § B.5](../REPORT.md#b5-node-exporter).
```
$ curl -s -o /dev/null -w '%{http_code}' localhost:9100/metrics
200
$ curl -s localhost:9090/api/v1/targets | jq -r '.data.activeTargets[] | "\(.labels.job) \(.scrapeUrl) \(.health)"'
node        http://host.docker.internal:9100/metrics  up
prometheus  http://localhost:9090/metrics             up
sensor-api  http://api:8000/metrics                   up
```
Right after startup, CPU busy % read 68 % while the load average was only 0.78.

#### Stage B5 — Grafana (provisioned) and the app dashboard

**Built:**
- The Grafana service.
- Datasource and dashboard provisioning files.
- `sensor-service.json`, with four rows:
  1. **Overview**: stat panels for stored readings/s, p95, 5xx share, 422 share, devices by status, and fault switches.
  2. **Application**: request rate by route, p50/p95/p99, responses by status code, Apdex, and average DB write time from the Summary.
  3. **Ingestion (business)**: readings by outcome, devices by status over time, anomalies per minute by reason, validation errors per minute by field, and anomaly share.
  4. **Faults and API process**: fault switches, faults injected per minute, requests in flight, and API CPU/memory.

Every panel has a description (the ⓘ icon) explaining what it shows.

**Verified:**
```
$ curl -s localhost:3000/api/health
{"database":"ok","version":"13.2.2",...}
$ curl -s localhost:3000/api/datasources/uid/prometheus | jq -c '{name,uid,url,readOnly}'
{"name":"Prometheus","uid":"prometheus","url":"http://prometheus:9090","readOnly":true}
$ curl -s 'localhost:3000/api/search?type=dash-db' | jq -c '.[] | {uid,title,folderTitle}'
{"uid":"node-host","title":"Host (Node Exporter)","folderTitle":"Sensor Ingestion"}
{"uid":"sensor-service","title":"Sensor Ingestion Service","folderTitle":"Sensor Ingestion"}
```

Every panel query was run through Grafana's own query API (`POST /api/ds/query`, the same path the browser uses) over the last 5 minutes, and all **23 queries in 20 panels returned data**:
```
ok  Stored readings / s                        last=3.87
ok  p95 latency · POST /readings               last=0.0249
ok  Server errors (5xx)                        last=0
ok  Rejected readings (422)                    last=0.0184
ok  Devices by status                          3 series  [anomaly 2, ok 18, stale 1]
ok  Latency percentiles · POST /readings       p50 0.0152 · p95 0.0249 · p99 0.0467
ok  Apdex · POST /readings (T = 25 ms)         last=0.977
ok  DB write time (average) · Summary          last=0.0089
ok  Readings by outcome                        4 series
ok  Anomalies per minute by reason             5 series  [temperature_high 12, battery_low 5.45, ...]
... (23/23 ok)
```
In the browser, the dashboard rendered the same values: *Stored readings / s 3.82 · p95 24.606 ms · Server errors 0.0% · Rejected 1.4% · Devices anomaly 2 / ok 17 / stale 2 · Fault injection delay off / error off*.

**Reproducibility:**
```
$ docker compose rm -sf grafana && docker volume rm esd_hw1_grafana-data && docker compose up -d grafana
$ curl -s localhost:3000/api/dashboards/uid/sensor-service | jq -c '{title: .dashboard.title, panels: ..., provisioned: .meta.provisioned, canSave: .meta.canSave}'
{"title":"Sensor Ingestion Service","panels":20,"provisioned":true,"canSave":false}
```

#### Stage B6 — Host dashboard

**Built:** `node-host.json` with 12 panels in three rows:
1. **Machine**: hostname, hardware, kernel, CPU count, RAM and uptime, as stat panels.
2. **CPU and memory**: CPU busy by mode (stacked), load average against the CPU count, memory used vs available, and a bar gauge of filesystem usage.
3. **Disk and network**: NVMe read/write throughput, and network receive/transmit throughput on the physical interfaces (loopback and Docker bridges excluded).

**Verified:** all 18 queries return data through `POST /api/ds/query`. The dashboard renders:
```
Hostname  muhammad-affan-Latitude-5400      Hardware  Dell Inc. Latitude 5400
Kernel    7.0.0-34-generic                  CPUs 8    Memory 7.6 GiB
Filesystem used   / (nvme0n1p5) 46.5%   /boot/efi (nvme0n1p1) 51.7%
Load 1m 0.90 vs 8 CPUs    Network rx on wlo1 ≈ 2.2 MB/s
```
CPU busy read 12 % averaged over 1 minute, while `top`'s one-second sample read about 4 %. They measure different windows, and the minute included Grafana starting up. The 68 % seen right after Node Exporter started (B4) had disappeared once the rate window filled.

#### Stage B7 — Checking the p95 against the logs

**Method:** a script took every `POST /readings` access-log line from the 60 s before time T, and calculated exact percentiles from their `duration_ms`. It then asked Prometheus for `histogram_quantile(q, sum by (le) (rate(..._bucket{route="/readings",method="POST"}[1m])))` evaluated at the same T (`/api/v1/query?time=T`), so both describe exactly the same requests.

**Run 1, original buckets (5, 10, 25, 50, 100, 250, 500 ms, …), 234 requests:**
```
p50  logs(exact) =   15.64 ms   histogram_quantile =   15.65 ms
p95  logs(exact) =   20.86 ms   histogram_quantile =   24.56 ms
p99  logs(exact) =   25.43 ms   histogram_quantile =   44.30 ms
share <= 0.01s:  logs 0.214   prometheus 0.215
share <= 0.025s: logs 0.974   prometheus 0.972
share <= 0.05s:  logs 0.996   prometheus 0.995
```
The bucket shares agree to within 0.002, so the counts are right. The p95 and p99 errors come from interpolating inside wide buckets.

**Change:** I added bucket bounds at 15, 20, 30, 40 and 75 ms in `app/metrics.py`, and rebuilt the API.

**Run 2, finer buckets, 231 requests, window ending 10:34:41 UTC** (taken after a full minute of post-restart data):
```
p50  logs(exact) =   16.06 ms   histogram_quantile =   16.38 ms
p95  logs(exact) =   18.91 ms   histogram_quantile =   19.94 ms
p99  logs(exact) =   24.93 ms   histogram_quantile =   23.82 ms
```
p95 is now within 5 % and p99 within 4 % (it was 74 % too high before). Series from the app went from about 150 to 196, which is an acceptable cost. The full explanation is in [REPORT.md § B.3](../REPORT.md#b3-percentiles-and-time-window).

#### Stage B8 — Whole stack from one command

**Verified:** `docker compose down`, then `docker compose up -d --build`. All five services came back, with no manual steps:
```
$ docker compose ps --format '{{.Service}}\t{{.Status}}\t{{.Ports}}'
api            Up 17 seconds (healthy)   0.0.0.0:8000->8000/tcp
grafana        Up 17 seconds             0.0.0.0:3000->3000/tcp
node-exporter  Up 17 seconds             (host network, :9100)
prometheus     Up 17 seconds             0.0.0.0:9090->9090/tcp
simulator      Up 12 seconds

$ curl -s localhost:9090/api/v1/targets | jq -r '.data.activeTargets[] | "\(.labels.job) \(.health)"'
node up
prometheus up
sensor-api up

$ curl -s 'localhost:3000/api/search?type=dash-db' | jq -r '.[] | "\(.uid) \(.title)"'
node-host Host (Node Exporter)
sensor-service Sensor Ingestion Service

# metrics history survived down/up (prometheus-data volume)
$ curl -s --get localhost:9090/api/v1/query --data-urlencode 'query=count_over_time(up{job="sensor-api"}[30m])'
165 samples of up{job=sensor-api} in the last 30 min

$ docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}'
grafana 241MiB · api 42MiB · prometheus 38MiB · simulator 23MiB · node-exporter 9MiB
```
The monitoring stack adds about 290 MB of RAM. `.venv/bin/pytest -q` passes: 83 tests (12 of them for metrics). That's the `rate()` warm-up effect from B3 again: `node_cpu_seconds_total` had only a few seconds of samples in its 1-minute window.

$ curl -s localhost:8000/metrics | grep '^# TYPE sensor_'
# TYPE sensor_http_requests_total counter
# TYPE sensor_http_request_duration_seconds histogram
# TYPE sensor_http_requests_in_progress gauge
# TYPE sensor_readings_total counter
# TYPE sensor_reading_validation_errors_total counter
# TYPE sensor_anomalies_total counter
# TYPE sensor_db_write_duration_seconds summary
# TYPE sensor_faults_injected_total counter
# TYPE sensor_fault_active gauge
# TYPE sensor_devices gauge
```

#### Stage B9 — Screenshots for the report

**Built:**
- `scripts/capture_screenshots.sh`. It captures the Grafana dashboards, the single panels, the Prometheus target page and the operator dashboard with headless Chrome, using a throwaway profile, then crops away empty background.
- It uses a fixed, absolute time window. The first attempt used `from=now-30m&to=now`, and every `rate()` panel dipped at the right-hand edge. Headless Chrome's `--virtual-time-budget` runs the page clock ahead of real time, so "now" was past the last scrape. Prometheus confirmed the real rate was a steady 3.95/s at that moment.
- A dashboard fix found while reviewing the screenshots: *Fault switches*, *Faults injected per minute* and *Requests in flight* showed a meaningless 0–100 axis when every value was 0. They now have `axisSoftMax` values of 1, 10 and 3. The latency panel's description also listed the old bucket bounds, and now lists the current ones.

**Verified:**
```
$ scripts/capture_screenshots.sh 30 "2026-09-26 11:43:26 UTC"
window: 2026-09-26 16:13:26 PKT -> 16:43:26 PKT
  docs/screenshots/b_app_dashboard.png
  ... (9 files)
```
The spikes between 16:34 and 16:37 PKT in the screenshots are manual test readings sent from the operator dashboard. The logs confirm them by their request-ID prefix:
```
$ docker compose logs api --no-log-prefix --since 20m | jq -r 'select(.event=="reading_rejected" and (.request_id|startswith("ui-"))) | "\(.timestamp) \(.errors|map(.field+":"+.type)|join(","))"'
2026-09-26T11:35:30.542Z device_id:string_pattern_mismatch
2026-09-26T11:36:22.554Z humidity_pct:less_than_equal
...
$ ... select(.event=="anomaly_detected" and (.request_id|startswith("ui-"))) ...
2026-09-26T11:34:28.636Z dev-100 temperature_high
2026-09-26T11:35:23.804Z dev-100 temperature_high
...
```

---

## Part C: logs

#### Stage C0 — Prerequisites and Docker log rotation

**Checked:**
- 2.5 GiB of RAM was available.
- `vm.max_map_count=1048576`, which is at least the 262,144 Elasticsearch needs, so no `sudo` was required.
- Latest Elastic 9.x images: `elasticsearch:9.5.3`, `kibana:9.5.3` and `elastic/filebeat:9.5.3`. All three are pinned to the same version.

**Built:** an `x-app-logging` anchor in `docker-compose.yml` (`json-file`, `max-size: 10m`, `max-file: 3`), applied to `api` and `simulator`. Before this, Docker's log files had no size limit.
```
$ docker inspect <api> --format '{{.HostConfig.LogConfig.Type}} {{json .HostConfig.LogConfig.Config}}'
json-file {"max-file":"3","max-size":"10m"}   path=/var/lib/docker/containers/aff99987…/aff99987…-json.log
```

#### Stage C1 — Elasticsearch

**Built:** service `elasticsearch`:
- single node, security off, heap fixed at 512 MB,
- volume `es-data`,
- port **127.0.0.1**:9200 only,
- a healthcheck waiting for `yellow`.
```
$ curl -s localhost:9200 | jq -c '{cluster_name, version: .version.number}'
{"cluster_name":"sensor-logs","version":"9.5.3"}
$ curl -s localhost:9200/_cluster/health | jq -c '{status, number_of_nodes}'
{"status":"green","number_of_nodes":1}
heap_max_mb=512 · container memory 844 MiB · docker port → 127.0.0.1:9200
```

#### Stage C2 — Retention policy and index template

**Built:**
- `monitoring/elasticsearch/ilm-policy.json`: hot phase rolls over at `max_age: 10m` or 1 GB; delete phase deletes 1 h after rollover.
- `monitoring/elasticsearch/index-template.json`: pattern `sensor-logs*`, a data stream, 1 shard / 0 replicas, and `dynamic: false` with 29 explicitly typed fields.
- The ILM poll interval is set to 1 m.

**Verified** on a throwaway data stream, `sensor-logs-test`:
```
policy: {"hot":{"max_age":"10m","max_primary_shard_size":"1gb"},"delete_after":"1h"}
template: {"patterns":["sensor-logs*"],"lifecycle":"sensor-logs-policy","dynamic":false,"fields":29}
POST sensor-logs-test/_doc {…,"errors":[{"field":"humidity_pct",…}],"password":"hunter2"}
  → created in .ds-sensor-logs-test-2026.09.26-000001, policy sensor-logs-policy attached
mapping has "password"?                     false
term errors.field:humidity_pct              1 hit
match password:hunter2                      0 hits   (not indexed)
_source.password                            "hunter2" (but still STORED)
```
**Lesson:** `dynamic: false` limits what is *searchable*, not what is *stored*. Keeping secrets out is the app's job: its field whitelist means they're never written.

#### Stage C3 — Filebeat

**Built:** `monitoring/filebeat/filebeat.yml`:
- Docker autodiscover plus a `filestream` input per matching container, using the `container` parser.
- Four processors: `copy_fields` (message → `log.original`), `decode_json_fields` (to the root, `overwrite_keys`, `add_error_key: false`), `timestamp` (from our `timestamp`), and `drop_fields`.
- Output to the `sensor-logs` data stream; Filebeat's own template and ILM are switched off.
- The service runs as root with read-only mounts of `/var/lib/docker/containers` and `docker.sock`, plus volume `filebeat-data` for the registry.

**Problems found and fixed:**
1. **No inputs started.** A condition on `docker.container.labels.com_docker_compose_service` never matched. A debug run (`-d autodiscover`) showed that Filebeat nests the dotted label keys (`container.labels.com.docker.compose.project.value`), and `labels.dedot: true` didn't change that. The fix was to match on the name Compose gives each container: `regexp: container.name: "^esd_hw1-(api|simulator)-[0-9]+$"`. Then: `Input 'filestream' starting` ×2, and `Connection to backoff(elasticsearch(http://elasticsearch:9200)) established`.
2. **Personal data in every document.** Autodiscover attached `docker.container.labels`, including `com_docker_compose_project_working_dir: /home/<user>/…`, which contains the user's name. I added `docker` to `drop_fields`, then deleted the data stream and the Filebeat registry so everything was re-ingested from Docker's log files.

   I also found that a check using `exists`/`query_string` returned 0 even while the field was present, because those queries only see *indexed* fields. The reliable check is to scan `_source`:
```
docs: 9248 · sources with a docker key: 0 · sources mentioning /home/: 0
distinct top-level keys: @timestamp anomaly_reasons battery_pct config container device_id duration_ms errors event fault
                         host humidity_pct is_anomaly level log logger message method path reading_id request_id service
                         stats status_code stream temperature_c
docs by container: esd_hw1-api-1 8865 · esd_hw1-simulator-1 99      (only our two services)
docs by event: http_request 4528, reading_stored 3991, anomaly_detected 277, reading_rejected 64, sim_send_failed 64, sim_summary 34, …
```

#### Stage C4 — Kibana, provisioned

**Built:**
- Service `kibana` (127.0.0.1:5601, with a healthcheck on `/api/status`).
- One-shot service `logs-setup` (`curlimages/curl:8.16.0`) running `monitoring/logs-setup.sh`: poll interval, policy, template, then an import of `monitoring/kibana/saved-objects.ndjson` (the data view `sensor-logs` plus 7 saved searches) and setting the default data view.
- `filebeat` has `depends_on: logs-setup: service_completed_successfully`.
```
[logs-setup] importing Kibana saved objects (data view + saved searches)
{"successCount":8,"success":true,"warnings":[],…}
[logs-setup] done                     exit code: 0
data views: {"id":"sensor-logs","name":"Sensor logs","title":"sensor-logs"} · default: sensor-logs · 37 fields
saved searches (query → hits at the time):
  Errors and warnings        level : ("ERROR" or "WARNING")                          460
  Server errors (5xx)        event : "http_request" and status_code >= 500          0
  Slow requests (> 100 ms)   event : "http_request" and duration_ms > 100           21
  Rejected readings (422)    event : "reading_rejected"                              73
  Anomalies                  event : "anomaly_detected"                              314
  Fault injection            event : ("fault_injected" or "fault_config_changed")    0
  Simulator send failures    service : "simulator" and event : "sim_send_failed"     73
```
Kibana 9 migrated the imported searches to its new `tabs` format; the queries were kept intact.

**A real incident found through the logs:** the "Slow requests" search found 20 `POST /readings` requests of up to 424 ms, all between 17:15 and 17:17 UTC, just after Elasticsearch started and while the Kibana and Filebeat images were being unpacked. Part B's host metrics explain them:
```
UTC       disk write   p95 POST /readings
17:05      1.2 MB/s     19.9 ms
17:15:30  29.5 MB/s     36.1 ms
17:16:30  10.0 MB/s     54.6 ms
17:20      0.7 MB/s     23.7 ms
```
The logs found *which* requests were slow; the metrics showed *why* (disk contention slowing SQLite's flushes to disk).

**Housekeeping:** the "Help us improve the Elastic Stack" banner was hidden in the screenshots. `TELEMETRY_BANNER` isn't among the settings Kibana's Docker image accepts from the environment; `TELEMETRY_ALLOWCHANGINGOPTINSTATUS=false` (with `TELEMETRY_OPTIN=false`) is.

#### Stage C5 — Worked example: raw line → stored document → Kibana search

I sent `POST /readings` with `X-Request-ID: c5-demo-1` and 41.5 °C, which returned `201` with `["temperature_high"]`.
```
1. app stdout (docker compose logs api):
{"timestamp": "2026-09-26T17:28:33.009Z", "level": "WARNING", "service": "sensor-api", "logger": "app", "event": "anomaly_detected", "message": "anomaly detected: temperature_high", "request_id": "c5-demo-1", "device_id": "dev-100", "reading_id": 116760, "anomaly_reasons": ["temperature_high"], "temperature_c": 41.5, "humidity_pct": 45.0, "battery_pct": 88.0}

2. Docker's file /var/lib/docker/containers/aff99987…/aff99987…-json.log (read through filebeat's read-only mount):
{"log":"{\"timestamp\": \"2026-09-26T17:28:33.009Z\", … \"battery_pct\": 88.0}\n","stream":"stdout","time":"2026-09-26T17:28:33.01002747Z"}

3. stored document (.ds-sensor-logs-2026.09.26-000001), _source abridged:
{"@timestamp":"2026-09-26T17:28:33.009Z","level":"WARNING","service":"sensor-api","event":"anomaly_detected",
 "message":"anomaly detected: temperature_high","request_id":"c5-demo-1","device_id":"dev-100","reading_id":116760,
 "anomaly_reasons":["temperature_high"],"temperature_c":41.5,"humidity_pct":45,"battery_pct":88,"stream":"stdout",
 "container":{"name":"esd_hw1-api-1","image":{"name":"sensor-api:dev"},"id":"aff99987…"},"host":{"name":"911f183df3ff"},
 "log":{"original":"{\"timestamp\": \"2026-09-26T17:28:33.009Z\", … }\n"}}
field types: request_id keyword · level keyword · event keyword · temperature_c float · anomaly_reasons keyword · log.original keyword (not indexed)

4. all documents for the request (request_id : "c5-demo-1"): reading_stored (INFO) · anomaly_detected (WARNING) · http_request (INFO, duration_ms 6.53)
```
`@timestamp` is the app's time (…33.009Z), not Docker's (…33.010Z). The screenshots are `docs/screenshots/c_kibana_request_id.png`, `c_kibana_document.png`, `c_kibana_warnings.png` and `c_kibana_rejected_saved_search.png`.

#### Stage C6 — No secrets or personal data in the pipeline

I planted fake values in every place a client can put them:
- an `Authorization: Bearer SECRETTOKEN-c6` header,
- a `Cookie: session=SECRETCOOKIE-c6` header,
- extra body fields `"password":"SECRETPASSWORD-c6"` and `"owner_email":"jane.doe@example.com"` (returned 201),
- a password inside a body rejected with 422,
- `GET /devices?token=SECRETQUERY-c6`.
```
stored for these requests: reading_stored · http_request path=/readings 201 · reading_rejected errors=[{humidity_pct, less_than_equal}] ·
                           http_request 422 · http_request path=/devices 200   (route template only: no query string)
scanned 11343 documents' _source incl. log.original:
{'SECRETTOKEN': 0, 'SECRETCOOKIE': 0, 'SECRETPASSWORD': 0, 'SECRETQUERY': 0, 'jane.doe': 0, 'example.com': 0,
 'Bearer': 0, 'password': 0, 'owner_email': 0, '/home/': 0}
raw Docker logs of api: 0 for each value
```
None of the Filebeat processors adds data: `copy_fields` copies our own line, `decode_json_fields` parses it, `timestamp` re-uses a field, and `drop_fields` only removes. Autodiscover's Docker labels, which did contain a home path (C3), are dropped.

#### Stage C7 — What survives what, and retention

| Action | Docker log (`docker compose logs api`) | Elasticsearch | Notes |
|---|---|---|---|
| `docker compose restart api` | c5-demo-1: 3 → 3 | 3 → 3 | same container, same log file |
| `docker compose up -d --force-recreate api` | 3 → **0** | 3 → **3** | the new container starts a new, empty log file. Autodiscover started an input for it within about 6 s (165 documents) |
| `docker compose restart filebeat` | — | c5-demo-1 3 → 3 | **no duplicates**: the registry (`filebeat-data`) remembers the read position |
| Filebeat stopped for 20 s while the app logged `c7-while-down` | 2 lines | 0 while stopped → **2** after start | nothing lost; Filebeat resumed from its saved position. (The total rose 12038 → 12083 at first: documents already in flight became searchable after the 1 s refresh.) |
| `docker compose restart elasticsearch` | — | 12350 → 12350 | `es-data` volume |
| `docker compose down` then `up -d --build` | recreated (empty) | 18851 → 18908 and growing; c5-demo-1 still 3 | about 2 min to be fully up. Order: elasticsearch healthy → logs-setup (exit 0) → filebeat |

**Retention (ILM):**
```
.ds-sensor-logs-2026.09.26-000001  created 17:25:43Z · rolled over 17:36:20Z (10.6 min: 10m + ≤1m poll) · 14652 docs
.ds-sensor-logs-2026.09.26-000002  created 17:36:20Z · rolled over 17:46:23Z
.ds-sensor-logs-2026.09.26-000003  created 17:46:23Z (current write index)
explain -000001: {"phase":"hot","action":"complete","age":"10.93m", since rollover: 10 min} → delete due ≈ 18:36–18:37Z
```

**The deletion, observed** by a watcher polling `_cat/indices` every 10 s and `_ilm/explain` every 10 min. It skips any check where Elasticsearch doesn't answer:
```
{"t":"2026-09-26T18:28:06Z","phase":"hot","action":"complete","age":"51.76m"}
{"t":"2026-09-26T18:38:09Z","phase":"delete","action":"delete","age":"1.03h"}
000001 GONE at 2026-09-26T18:38:29Z
index                             docs.count creation.date.string
.ds-sensor-logs-2026.09.26-000002       4256 2026-09-26T17:36:20.748Z
.ds-sensor-logs-2026.09.26-000003       6429 2026-09-26T17:46:23.333Z
.ds-sensor-logs-2026.09.26-000004       5729 2026-09-26T17:57:23.287Z
.ds-sensor-logs-2026.09.26-000005       5707 2026-09-26T18:08:23.310Z
.ds-sensor-logs-2026.09.26-000006       5714 2026-09-26T18:19:23.242Z
.ds-sensor-logs-2026.09.26-000007       3723 2026-09-26T18:30:23.281Z
{"generation":8,"indices":[…-000002 … -000007]}
c5-demo-1 docs now: 0
```
The rollovers came every 10–11 min (17:36, 17:46, 17:57, 18:08, 18:19, 18:30), which is `max_age: 10m` plus up to one 1-minute ILM check. `-000001` lived 17:25:43 → 18:38:29, i.e. 1 h 13 min.
**Memory with the whole stack running:** Kibana 1.73 GiB, Elasticsearch 1.01 GiB, Grafana 403 MiB, Filebeat 92 MiB, Prometheus 65 MiB, API 42 MiB. Host RAM available: 939 MiB.

**A watcher mistake worth recording:** my first deletion watcher reported "000001 GONE at 17:44:36Z". That was during `docker compose down`, when Elasticsearch was unreachable, and the check "index not listed" can't tell "deleted" from "no answer". The index was still there after `up`. The watcher now skips a check when Elasticsearch doesn't answer.

#### Stage C8 — Localhost-only ports (security fix)

**Found:** the API (8000, including `/admin/faults`), Grafana (3000) and Prometheus (9090) listened on `0.0.0.0` and `[::]`, and Node Exporter on `*:9100`. That made all four reachable from the Wi-Fi address (192.168.1.8). Elasticsearch and Kibana were already on 127.0.0.1.

**Fixed:**
- `ports: "127.0.0.1:…"` for `api`, `prometheus` and `grafana`.
- `--web.listen-address=172.17.0.1:9100` for `node-exporter`, which runs on the host network. That's Docker's bridge address, which `host.docker.internal` resolves to inside `prometheus`.
```
listeners:  127.0.0.1:3000  127.0.0.1:5601  127.0.0.1:8000  127.0.0.1:9090  127.0.0.1:9200  172.17.0.1:9100
from the laptop:     localhost:8000/health 200 · localhost:3000/api/health 200 · localhost:9090/-/ready 200 · 172.17.0.1:9100/metrics 200
via 192.168.1.8:     8000 000 · 3000 000 · 9090 000 · 9100 000 · 9200 000 · 5601 000   (connection refused)
prometheus targets:  node up · prometheus up · sensor-api up
```

#### Stage C9 — Searching for errors (a 30-second error fault)

`PUT /admin/faults {"error_every_n":10}` at 18:54:26.204Z (`request_id` c-error-on), then `DELETE` at 18:54:56.243Z (c-error-off).
```
level:ERROR, by event:              fault_injected 11 · http_request 11           (22 documents, all sensor-api)
event:http_request and status>=500: 11                                           (was 0 before)
simulator sim_send_failed 500:      11
one failed request, sim-e2306ccbc8f2:
  18:54:28.541Z sensor-api ERROR   fault_injected  "injected error"
  18:54:28.543Z sensor-api ERROR   http_request    "POST /readings -> 500"  status 500
  18:54:28.550Z simulator  WARNING sim_send_failed "reading not accepted: HTTP 500"  dev-014
fault_config_changed: 18:54:26.204Z c-error-on {error_every_n:10} · 18:54:56.243Z c-error-off {error_every_n:0}
```
Counted 8 s after the fault ended, the first numbers were 10 / 10 / 11, and only the "on" config change had arrived. Re-counted a little later, everything was 11 / 11 / 11 and both changes were there. Filebeat and the index refresh add a few seconds of delay. The screenshots are `c_kibana_errors.png` and `c_kibana_error_trace.png`.

---

## Part D: system design

#### Stage D1 — Failure modes, tested one component at a time

**Method:** `d1_fail.sh <service> <seconds>` (a scratch script):
1. `docker compose stop <service>`.
2. Probe: API `POST /readings` with `X-Request-ID: d1-<service>`, `GET /devices`, `/`, `/metrics`; Prometheus ready and targets; a Grafana `/api/ds/query` for `up`; the Elasticsearch count; Kibana `/api/status`.
3. Wait, and count the simulator's `sim_send_failed` during the outage.
4. `docker compose start`, wait until healthy, and check whether the tagged reading reached Elasticsearch and how many `up{job="sensor-api"}` samples exist in the window.

`000` means connection refused. (The Elasticsearch count falls between runs as retention deletes old backing indices.)

**Prometheus** (down 35 s):
```
=== stop prometheus at 19:21:53
--- while prometheus is down:
  api POST /readings : 201
  api GET /devices   : 200    dashboard GET / : 200    /metrics : 200
  prometheus ready   : 000    targets: 
  grafana query 'up' : ERROR: Post "http://prometheus:9090/api/v1/query": dial tcp: lookup prometheus on 127.0.0.11:53: 
  elasticsearch docs : 32434    kibana /api/status : 200
--- simulator during the outage:       1 422 
=== start prometheus at 19:22:29 (down ~35 s)
--- after recovery (19:22:46):
  tagged reading d1-prometheus in elasticsearch: 2 docs
  prometheus samples of up{job=sensor-api} in the last 70 s: 6 (expected 14 at 5 s)
  sensor-api up=0 samples in that window: 0
  api POST /readings : 201
  prometheus ready   : 200    targets: node=up prometheus=up sensor-api=up
  grafana query 'up' : ok (3 series)
  elasticsearch docs : 32764    kibana /api/status : 200
```
**Grafana** (down 25 s):
```
=== stop grafana at 19:23:05
--- while grafana is down:
  api POST /readings : 201
  api GET /devices   : 200    dashboard GET / : 200    /metrics : 200
  prometheus ready   : 200    targets: node=up prometheus=up sensor-api=up
  grafana query 'up' : 
  elasticsearch docs : 33029    kibana /api/status : 200
--- simulator during the outage:       1 422 
=== start grafana at 19:23:31 (down ~25 s)
--- after recovery (19:23:52):
  tagged reading d1-grafana in elasticsearch: 2 docs
  prometheus samples of up{job=sensor-api} in the last 60 s: 12 (expected 12 at 5 s)
  sensor-api up=0 samples in that window: 0
  api POST /readings : 201
  prometheus ready   : 200    targets: node=up prometheus=up sensor-api=up
  grafana query 'up' : ok (3 series)
  elasticsearch docs : 33386    kibana /api/status : 200
```
**Node Exporter** (down 25 s):
```
=== stop node-exporter at 19:23:53
--- while node-exporter is down:
  api POST /readings : 201
  api GET /devices   : 200    dashboard GET / : 200    /metrics : 200
  prometheus ready   : 200    targets: node=down prometheus=up sensor-api=up
  grafana query 'up' : ok (3 series)
  elasticsearch docs : 33473    kibana /api/status : 200
--- simulator during the outage: 
=== start node-exporter at 19:24:18 (down ~25 s)
--- after recovery (19:24:38):
  tagged reading d1-node-exporter in elasticsearch: 2 docs
  prometheus samples of up{job=sensor-api} in the last 60 s: 12 (expected 12 at 5 s)
  sensor-api up=0 samples in that window: 0
  api POST /readings : 201
  prometheus ready   : 200    targets: node=up prometheus=up sensor-api=up
  grafana query 'up' : ok (3 series)
  elasticsearch docs : 33817    kibana /api/status : 200
node samples in last 60 s: 12, of which up=0: 6
```
**Elasticsearch** (down 50 s). Filebeat's own log during the outage, and the documents per 10 s indexed afterwards:
```
=== stop elasticsearch at 19:24:59
--- while elasticsearch is down:
  api POST /readings : 201
  api GET /devices   : 200    dashboard GET / : 200    /metrics : 200
  prometheus ready   : 200    targets: node=up prometheus=up sensor-api=up
  grafana query 'up' : ok (3 series)
  elasticsearch docs :     kibana /api/status : 000
--- simulator during the outage:       4 422 
=== start elasticsearch at 19:25:56 (down ~50 s)
--- after recovery (19:26:47):
  tagged reading d1-elasticsearch in elasticsearch: 2 docs
  prometheus samples of up{job=sensor-api} in the last 85 s: 17 (expected 17 at 5 s)
  sensor-api up=0 samples in that window: 0
  api POST /readings : 201
  prometheus ready   : 200    targets: node=up prometheus=up sensor-api=up
  grafana query 'up' : ok (3 series)
  elasticsearch docs : 34956    kibana /api/status : 200
filebeat: 19:25:07 error failed to perform any bulk index operations … lookup elasticsearch … server misbehaving
          19:25:08 info  Connecting to backoff(elasticsearch(http://elasticsearch:9200))
          19:25:09 info  Attempting to reconnect to backoff(elasticsearch(http://elasticsearch:9200)) with 1 reconnect attempt(s)
docs per 10 s (19:24:50 → 19:26:40): 11 94 89 88 92 88 84 81 85 83 86 45   ← no gap: the outage's logs were delivered afterwards
```
**Kibana** (down 35 s):
```
=== stop kibana at 19:27:09
--- while kibana is down:
  api POST /readings : 201
  api GET /devices   : 200    dashboard GET / : 200    /metrics : 200
  prometheus ready   : 200    targets: node=up prometheus=up sensor-api=up
  grafana query 'up' : ok (3 series)
  elasticsearch docs : 35135    kibana /api/status : 000
--- simulator during the outage:       2 422 
=== start kibana at 19:27:45 (down ~35 s)
--- after recovery (19:28:43):
  tagged reading d1-kibana in elasticsearch: 2 docs
  prometheus samples of up{job=sensor-api} in the last 70 s: 14 (expected 14 at 5 s)
  sensor-api up=0 samples in that window: 0
  api POST /readings : 201
  prometheus ready   : 200    targets: node=up prometheus=up sensor-api=up
  grafana query 'up' : ok (3 series)
  elasticsearch docs : 35921    kibana /api/status : 200
Elasticsearch docs kept growing (35135 → 35921): ingestion doesn't need Kibana.
```
**API** (down 35 s):
```
=== stop api at 19:28:43
--- while api is down:
  api POST /readings : 000
  api GET /devices   : 000    dashboard GET / : 000    /metrics : 000
  prometheus ready   : 200    targets: node=up prometheus=up sensor-api=down
  grafana query 'up' : ok (3 series)
  elasticsearch docs : 36009    kibana /api/status : 200
--- simulator during the outage:     140 ConnectError       1 ConnectTimeout 
=== start api at 19:29:20 (down ~35 s)
--- after recovery (19:29:37):
  tagged reading d1-api in elasticsearch: 0 docs
  prometheus samples of up{job=sensor-api} in the last 70 s: 14 (expected 14 at 5 s)
  sensor-api up=0 samples in that window: 7
  api POST /readings : 201
  prometheus ready   : 200    targets: node=up prometheus=up sensor-api=up
  grafana query 'up' : ok (3 series)
  elasticsearch docs : 36162    kibana /api/status : 200
raw counter after the restart: sensor_readings_total{outcome="normal"} = 59.0   (reset to 0 at start)
sum(rate(sensor_readings_total[1m])) every 10 s: 19:28:43 3.82 · 19:28:53 3.35 · 19:29:03 2.72 · 19:29:13 2.09 · 19:29:23 1.45 · 19:29:33 1.15
```
**Simulator** (down 40 s):
```
after 40 s: devices {"stale":21} · sensor_devices gauge {"ok":"0","anomaly":"0","stale":"21"} · api health 200
simulator's last line (on SIGTERM): {"event":"sim_summary","message":"simulator final summary","stats":{"sent":24644,"status_201":24067,"status_422":389,"status_5xx":11,"status_other":0,"conn_errors":177}}
restarted: after 20 s, devices {"ok":20,"stale":1}   (dev-100 = a manual test device, silent since)
```
**Docker log buffer size:** the API's log file grew 5,456,744 bytes between the container starting (18:53:33Z) and 19:21:16Z, i.e. about **3.3 KB/s**. With `max-size: 10m` × `max-file: 3` that's about **30 MB ≈ 2.5 h** of API logs. That's how long Filebeat or Elasticsearch can be down before log lines are actually lost.

**Not tested, reasoned from configuration:**
- `logs-setup` failing: `filebeat` has `depends_on: logs-setup: condition: service_completed_successfully`, so Filebeat wouldn't start at all. Logs would wait in Docker's files, within the same 2.5 h window.
- Filebeat stopping: tested in C7 (caught up from its registry, no duplicates).

#### Stage D1b — Architecture diagram

**Built:**
- `docs/diagrams/architecture.mmd`: a Mermaid flowchart with every service, arrow, storage location and "stops →" note, taken from `docker-compose.yml`, `monitoring/` and the D1 results.
- `scripts/render_diagram.sh`, which renders it with headless Chrome and Mermaid 11 from jsDelivr: the SVG from `--dump-dom`, and the PNG from a 2× screenshot.

The first render used `flowchart LR` and was 2382×696, unreadable when scaled to page width. The switch to `TB` gives 3235×4925 at 2×.

**Simplified afterwards.** The version with a failure note in every box was too dense to read at a glance. The diagram now shows only the components, their groups (Clients, sensor-api, Metrics, Logs, plus the one-shot logs-setup) and one short label per arrow, drawn `LR` at 3540×1233 (2×). The failure behaviour and the storage and retention detail moved to REPORT D.1–D.2, where they were already listed as tables. The `docker.sock` and "Filebeat waits for logs-setup" arrows were dropped from the drawing; they're described in D.1's text.
```
$ scripts/render_diagram.sh
wrote docs/diagrams/architecture.svg
wrote docs/diagrams/architecture.png
```

#### Stage D2 — Follow a metric: `sensor_anomalies_total{reason="humidity_low"}`

This series was chosen because no simulated device produces `humidity_low`, and the counter had been reset to 0 by the D1 API restart. So a single reading moves it from 0 to 1, with nothing else mixed in.
```
BEFORE  app /metrics: sensor_anomalies_total{reason="humidity_low"} 0.0   prometheus: 0
POST /readings  X-Request-ID d2-follow-1  humidity_pct 10  → {"id":145080,"is_anomaly":true,"anomaly_reasons":["humidity_low"],"received_at":"2026-09-26T19:33:16.563Z"}
sent at 19:33:16.554
AFTER   app /metrics (immediately): sensor_anomalies_total{reason="humidity_low"} 1.0
prometheus shows 1 after 2.2 s
raw samples stored (query sensor_anomalies_total{reason="humidity_low"}[40s]):
  19:32:43 0 · 19:32:48 0 · 19:32:53 0 · 19:32:58 0 · 19:33:03 0 · 19:33:08 0 · 19:33:13 0 · 19:33:18 1
scrape: lastScrape 19:33:18.449, duration 0.0096 s, interval 5 s
panel query sum by (reason) (increase(sensor_anomalies_total{reason="humidity_low"}[1m])), every 5 s:
  19:33:16 0 · 19:33:21 1.05 · … (1.05 throughout) … · 19:34:11 1.05 · 19:34:16 0
Grafana /api/ds/query (the same panel expression), instant at 19:33:40:
  temperature_low 0 · temperature_high 12 · humidity_low 1.0282 · humidity_high 0 · battery_low 12
```
The screenshots are `d_prometheus_raw_counter.png` and `d_grafana_anomalies_by_reason.png`.

#### Stage D3 — Follow a log: the same reading's `anomaly_detected` line
```
1. app stdout:
{"timestamp": "2026-09-26T19:33:16.567Z", "level": "WARNING", "service": "sensor-api", "logger": "app", "event": "anomaly_detected", "message": "anomaly detected: humidity_low", "request_id": "d2-follow-1", "device_id": "dev-100", "reading_id": 145080, "anomaly_reasons": ["humidity_low"], "temperature_c": 22.0, "humidity_pct": 10.0, "battery_pct": 88.0}
2. Docker's file /var/lib/docker/containers/ae33aec25ed1…/ae33aec25ed1…-json.log:
{"log":"{\"timestamp\": \"2026-09-26T19:33:16.567Z\", … \"battery_pct\": 88.0}\n","stream":"stdout","time":"2026-09-26T19:33:16.567348287Z"}
3. filebeat (autodiscover started this container's input when the container was created):
2026-09-26T18:53:34.029Z Input 'filestream' starting id=container-ae33aec25ed19b02f7df5fcba7fe2ad3ba7a206f6374176a4729df02e39efc0a
4. stored: _index .ds-sensor-logs-2026.09.26-000017, _id odk136ABdEksZtjUOIBI
{"@timestamp":"2026-09-26T19:33:16.567Z","stream":"stdout","service":"sensor-api","device_id":"dev-100","temperature_c":22,"battery_pct":88,
 "event":"anomaly_detected","message":"anomaly detected: humidity_low","host":{"name":"31f3c20d3332"},"anomaly_reasons":["humidity_low"],
 "level":"WARNING","humidity_pct":10,"logger":"app","container":{"id":"ae33aec25ed1…","name":"esd_hw1-api-1","image":{"name":"sensor-api:dev"}},
 "request_id":"d2-follow-1","reading_id":145080}          (+ log.original = line 1)
elasticsearch had all 3 docs for d2-follow-1 after 6.6 s:
  19:33:16.567 INFO reading_stored · 19:33:16.567 WARNING anomaly_detected [humidity_low] · 19:33:16.568 INFO http_request POST /readings -> 201 (5.9 ms)
```
The screenshot is `d_kibana_follow_log.png`.

**Found while tracing (now an open question):** the backing indices are numbered with odd numbers only from `-000007` on. `_data_stream` reports `generation: 18` with 6 indices (`000007, 000009, …, 000017`). The failure store is disabled and has no indices (`"failure_store":{"enabled":false,"rollover_on_write":true,"indices":[]}`). `-000017` was created at 19:27:28Z, about 2 min later than the 10-minute schedule, right after the D1 Elasticsearch restart (19:25–19:26).

---

## Part E: experiments

Raw outputs are saved in `docs/results/`: `e1_load_<stage>.json` (the client's view), `e1_server_<stage>.json` (Prometheus and Elasticsearch over the stage window), `e1_predictions.md` and `e2_cardinality.json`.

#### Stage E1.1 — Repeatable load

**Built:**
- `scripts/load.py`: open-loop `POST /readings` at a fixed rate, from a thread pool of 32 workers. Each request is a normal reading for `dev-100` with `X-Request-ID: load-<stage>-NNNNN`. It prints a JSON summary with client-side percentiles.
- `scripts/e1_stage_stats.py <stage> <start> <end>`: computes the stage's server-side figures from Prometheus, evaluated at the stage end over `[duration − 15 s]` (the 15 s margin excludes the previous stage), plus log counts from Elasticsearch.
```
$ python3 scripts/load.py --stage dryrun --rate 5 --duration 10
{"stage": "dryrun", "sent": 50, "status_counts": {"201": 50}, "client_latency_ms": {"p50": 12.6, "p95": 15.2, "p99": 39.5, "max": 39.5, "mean": 12.4}}
```

#### Stage E1.2 — Baseline (faults off, simulator stopped)

The simulator was stopped at about 20:32:00Z, and I waited 70 s so its requests had left the 1-minute window. That way only `load.py` hits `POST /readings`.
```
$ python3 scripts/load.py --stage baseline --rate 5 --duration 150
{"stage": "baseline", "start_utc": "2026-09-26T20:33:09.564Z", "end_utc": "2026-09-26T20:35:39.365Z", "rate_per_s": 5.0, "sent": 750, "status_counts": {"201": 750}, "client_latency_ms": {"p50": 12.7, "p95": 13.9, "p99": 16.1, "max": 32.1, "mean": 11.6}}
$ python3 scripts/e1_stage_stats.py baseline 2026-09-26T20:33:09.564Z 2026-09-26T20:35:39.365Z
{"stage": "baseline", "window_utc": ["2026-09-26T20:33:09.564Z", "2026-09-26T20:35:39.365Z"], "promql_range": "134s", "server_requests": 670, "server_p50_s": 0.0119, "server_p95_s": 0.0147, "server_p99_s": 0.015, "server_mean_s": 0.0111, "apdex_T25ms": 1.0, "share_over_500ms": 0.0, "http_5xx": 0, "faults_injected_delay": 0, "fault_active_delay_max": 0.0, "db_write_mean_s": 0.0046, "logs_this_stage_http_request": 750, "logs_fault_injected": 0, "logs_http_request_over_400ms": 0, "logs_level_error": 0}
```

#### Stage E1.3 — Prediction

`docs/results/e1_predictions.md` was written at **20:36:33Z**, 17 s before the fault was switched on (20:36:50.732Z). It holds 17 numbered predictions (P1–P17), mostly numeric, derived from the baseline and the histogram's bucket bounds.

#### Stage E1.4 — Introduce the fault (`delay_ms=500, delay_every_n=5`)
```
fault on at 20:36:50.732Z: PUT /admin/faults {"delay_ms":500,"delay_every_n":5,"error_every_n":0}   (X-Request-ID e1-fault-on)
$ python3 scripts/load.py --stage fault --rate 5 --duration 150
{"stage": "fault", "start_utc": "2026-09-26T20:36:50.857Z", "end_utc": "2026-09-26T20:39:20.657Z", "rate_per_s": 5.0, "sent": 750, "status_counts": {"201": 750}, "client_latency_ms": {"p50": 13.0, "p95": 513.9, "p99": 514.7, "max": 528.5, "mean": 112.1}}
{"stage": "fault", "window_utc": ["2026-09-26T20:36:50.857Z", "2026-09-26T20:39:20.657Z"], "promql_range": "134s", "server_requests": 670, "server_p50_s": 0.0128, "server_p95_s": 0.875, "server_p99_s": 0.975, "server_mean_s": 0.1115, "apdex_T25ms": 0.8, "share_over_500ms": 0.2, "http_5xx": 0, "faults_injected_delay": 134, "fault_active_delay_max": 1.0, "db_write_mean_s": 0.0047, "logs_this_stage_http_request": 750, "logs_fault_injected": 149, "logs_http_request_over_400ms": 149, "logs_level_error": 0}
first delayed ids: load-fault-00004 load-fault-00009 load-fault-00014 load-fault-00019 load-fault-00024 load-fault-00029 load-fault-00034 load-fault-00039
delayed ids NOT ending in 4 or 9: 0
fault_injected for load-fault-* (no time bound): 150 · raw counter sensor_faults_injected_total{fault="delay"} 150.0
last delayed: load-fault-00749 at 20:39:20.662Z (5 ms after end_utc, which is why the time-bounded log count says 149)
requests_in_progress over the stage: max 2, avg 1.04 · scrape timestamps …188.449 …193.449 …198.449 (always .449 s past the second)
```

| # | Prediction | Result | |
|---|---|---|---|
| P1 | p50 unchanged, about 12 ms | 12.8 ms | ✔ |
| P2 | `histogram_quantile` p95 / p99 = 0.875 / 0.975 s | **0.875 / 0.975 s** | ✔ exactly |
| P3 | true p95 about 515 ms | client p95 513.9 ms, p99 514.7 ms | ✔ |
| P4 | mean about 111 ms | 111.5 ms | ✔ |
| P5 | Apdex 0.80 | 0.80 | ✔ |
| P6 | 20 % > 500 ms | 0.20 | ✔ |
| P7 | +150 delays, about 60/min | 150; panel plateau at 60/min | ✔ |
| P8 | `fault_active` 1 during the stage | max 1.0 | ✔ |
| P9 | no 5xx, rate still 5 /s | 0 5xx, 750/750 × 201 | ✔ |
| P10 | DB write unchanged | 4.7 ms (was 4.6) | ✔ |
| P11 | in-flight often 2 | **avg 1.04, max 2** | ✘: aliasing (see below) |
| P12 | `fault_config_changed` on and off | 20:36:50.762Z (e1-fault-on), 20:41:18.861Z (e1-fault-off) | ✔ |
| P13 | 150 `fault_injected`, 0 ERROR | 150, 0 | ✔ |
| P14 | 150 `http_request` > 400 ms, only in the fault stage | 149 in the window (150 in total); 0 in baseline and recovery | ✔ |
| P15 | delayed IDs end in 4 or 9 | all 150 | ✔ |

**Why P11 failed:**
- The load starts a request every 0.2 s from 20:36:50.857. The delayed ones (n = 4, 9, …) start at .657 past each second and sleep 500 ms, so they're in flight from .657 to .157.
- Prometheus scrapes at .449 every 5 s, always in the half-second when no delayed request is in flight.
- A gauge sampled every 5 s can systematically miss something that happens on a regular 1 s cycle (aliasing).

#### Stage E1.5 — Effect on a caller with a short timeout

The fault was still on, with the counter at 750. I sent 5 readings with `curl --max-time 0.25`, sharing one device timestamp and with temperatures 23.1–23.5; the 5th was request #755, a delayed one. Then I retried #5 with the identical body.
```
dev-100 reading_count before: 1579
e1-timeout-1 -> HTTP 201 after 0.005296s
e1-timeout-2 -> HTTP 201 after 0.005325s
e1-timeout-3 -> HTTP 201 after 0.005542s
e1-timeout-4 -> HTTP 201 after 0.005259s
e1-timeout-5 -> HTTP 000 after 0.250673s        ← the client gave up
e1-timeout-5-retry -> HTTP 201 after 0.012265s
dev-100 reading_count after: 1585                ← 5 readings sent, 6 stored
server logs:
  20:40:34.260 e1-timeout-5        WARNING fault_injected
  20:40:34.542 e1-timeout-5-retry  INFO    reading_stored 160160
  20:40:34.547 e1-timeout-5-retry  INFO    http_request 201 12.4 ms
  20:40:34.770 e1-timeout-5        INFO    reading_stored 160161          ← the original is stored after all
  20:40:34.773 e1-timeout-5        INFO    http_request 201 514.14 ms     ← the server logs success
stored: {"id":160161,"timestamp":"2026-09-26T20:40:34.000Z","temperature_c":23.5}, {"id":160160,…same…}
```

#### Stage E1.6 — Recovery
```
fault off at 20:41:18.848Z: DELETE /admin/faults   (X-Request-ID e1-fault-off)
$ python3 scripts/load.py --stage recovery --rate 5 --duration 150
{"stage": "recovery", "start_utc": "2026-09-26T20:41:18.961Z", "end_utc": "2026-09-26T20:43:48.761Z", "rate_per_s": 5.0, "sent": 750, "status_counts": {"201": 750}, "client_latency_ms": {"p50": 12.7, "p95": 13.9, "p99": 17.6, "max": 34.5, "mean": 11.8}}
{"stage": "recovery", "window_utc": ["2026-09-26T20:41:18.961Z", "2026-09-26T20:43:48.761Z"], "promql_range": "134s", "server_requests": 670, "server_p50_s": 0.0119, "server_p95_s": 0.0147, "server_p99_s": 0.015, "server_mean_s": 0.0112, "apdex_T25ms": 1.0, "share_over_500ms": 0.0, "http_5xx": 0, "faults_injected_delay": 0, "fault_active_delay_max": 0.0, "db_write_mean_s": 0.0046, "logs_this_stage_http_request": 750, "logs_fault_injected": 0, "logs_http_request_over_400ms": 0, "logs_level_error": 0}
Grafana panel p95 ([1m]) every 10 s after the DELETE:
20:41:08 850ms 20:41:18 850ms 20:41:28 29ms 20:41:38 21ms 20:41:48 15ms 20:41:58 15ms … 20:42:48 15ms
```
**P16 ✔:** the recovery numbers are identical to the baseline.

**P17 ✘:** I predicted about 60 s to recover; it took about 10 s. Between the fault stage's end (20:39:20) and the `DELETE`, there was no load apart from the 6 timeout-test requests. So the 1-minute window held just 1 slow request, and the first 50 fast recovery requests pushed it below the 95th percentile. With the load continuing across the switch, slow requests would have stayed above 5 % of the window for about 45 s.

The screenshots, over 20:32:30–20:44:30Z, are `e_grafana_latency_three_stages.png`, `e_grafana_apdex_three_stages.png`, `e_grafana_faults_injected.png` and `e_kibana_slow_requests.png`. The simulator was restarted afterwards.

#### Stage E2.1 — The throwaway exporter and its wiring

**Built:**
- `scripts/cardinality_demo.py`: its own `CollectorRegistry()`, so there are no default collectors and it isn't the app's registry. It has a `demo_requests_total` counter, with `--mode label` (one series per `req-0000…0099`, hard cap `MAX_IDS = 100`) or `--mode nolabel`, and serves `:8001`.
- A Compose service `cardinality-demo` in profile **`e2`**, so it isn't started by `up`. It uses the `sensor-api:dev` image, mounts `./scripts` read-only, and takes `DEMO_MODE=label|nolabel`.
- A Prometheus job `cardinality-demo` with `file_sd_configs` on `monitoring/prometheus/file_sd/*.json` (refresh 5 s). The file holds `[]` except during the demo. `prometheus` mounts `file_sd/` read-only and was recreated once, keeping its history (358 samples of `up` in 30 min).
- `scripts/cardinality_demo.sh`, which runs the sequence and always cleans up (a `trap` removes the container and resets the targets file to `[]`).
```
docker compose config --services | grep -c cardinality-demo          → 0 (not in the default stack)
docker compose --profile e2 config --services | grep -c cardinality-demo → 1
cardinality-demo targets with the file at []: 0 (nothing shown as down)
```

#### Stages E2.2–E2.3 — Series growth, then removing the label

The first run, `scripts/cardinality_demo.sh`:
```
stage          t          count(demo_requests_total)  sum  count(last_over_time(…[30m]))  samples scraped  prometheus_tsdb_head_series
before         20:46:39Z  0                           0    0                              0                2830
with_label     20:46:51Z  100                         100  100                            100              2972
without_label  20:47:04Z  1                           100  101                            1                3009
after_cleanup  20:47:16Z  0                           0    101                            0                3009
series Prometheus stored: demo_requests_total{request_id="req-0000"} 1, …{request_id="req-0001"} 1, …
afterwards: targets file [], demo container gone, 0 demo_* series in job sensor-api
```
The second run, after fixing the "series added" query to `max_over_time(scrape_series_added{job="cardinality-demo"}[20s])`:
```
{"stage": "before", "t": "2026-09-26T20:47:59Z", "count(demo_requests_total)": "0", "sum(demo_requests_total)": "0", "count(last_over_time(demo_requests_total[30m]))": "101", "max scrape_series_added (20s)": "0", "scrape_samples_scraped": "0", "prometheus_tsdb_head_series": "3009"}
{"stage": "with_label", "t": "2026-09-26T20:48:12Z", "count(demo_requests_total)": "100", "sum(demo_requests_total)": "100", "count(last_over_time(demo_requests_total[30m]))": "101", "max scrape_series_added (20s)": "100", "scrape_samples_scraped": "100", "prometheus_tsdb_head_series": "3045"}
{"stage": "without_label", "t": "2026-09-26T20:48:24Z", "count(demo_requests_total)": "1", "sum(demo_requests_total)": "100", "count(last_over_time(demo_requests_total[30m]))": "101", "max scrape_series_added (20s)": "100", "scrape_samples_scraped": "1", "prometheus_tsdb_head_series": "3081"}
{"stage": "after_cleanup", "t": "2026-09-26T20:48:37Z", "count(demo_requests_total)": "0", "sum(demo_requests_total)": "0", "count(last_over_time(demo_requests_total[30m]))": "101", "max scrape_series_added (20s)": "1", "scrape_samples_scraped": "0", "prometheus_tsdb_head_series": "3081"}
```
The second run's `before` already shows **101** historical series left from the first run: nothing had deleted them. Its first labelled scrape reports `scrape_series_added` = **100**. The in-memory series count grew by only 36 this time (3009 → 3045), because the same label sets were already in memory from the first run. The first run's +142 (2830 → 2972) was the real cost: 100 demo series, plus the new target's `up`/`scrape_*` series and other churn.

The screenshot is `e_prometheus_series_count.png`.



---

# Assignment 2

Stage evidence for A2. Full outputs are in `docs/results/`; decisions in `docs/DECISIONS.md`.

## A2-A. HTTP API (2026-10-25)

- Built: problem+json errors (`app/problems.py`), shared ingest rule (`app/ingest.py`), idempotent insert with unique index `(device_id, recorded_at)`, per-device token bucket (`app/ratelimit.py`), `GET /readings/{id}` + `Location`, validated `device_id` path params, committed OpenAPI (`docs/openapi.json`), `clients/http_caller.py`.
- `.venv/bin/pytest -q` → `102 passed`.
- `python -m clients.http_caller --demo` → success, retry after a 500, three timeouts then `breaker: closed -> open`, three blocked calls, half-open trial closes it (`a_caller_demo.txt`). Server log for the timed-out call: stored once (reading 6253), both retries `reading_replayed` (`a_caller_call5_server_log.txt`).
- `scripts/rate_limit_demo.sh` → 5 × 201, 2 × 429 `Retry-After: 1`, 201 after 1 s (`a_rate_limit_demo.txt`).
- `scripts/http_examples.sh` → 201 / 200 replay / 409 / 422 (`a_http_examples.txt`).

## A2-B. gRPC (2026-10-25)

- Built: `proto/sensor_ingest.proto`, `scripts/gen_proto.sh`, `app/grpc_server.py` (interceptor + servicer), Compose service `grpc`, `clients/grpc_caller.py`, Filebeat match + index-template fields (`docker compose run --rm logs-setup`, then `POST sensor-logs/_rollover` so the new mapping applies now).
- `.venv/bin/pytest -q tests/test_grpc.py` → `7 passed` (incl. HTTP-stored reading replayed over gRPC).
- `scripts/both_transports.sh` → HTTP 201/422, gRPC OK/INVALID_ARGUMENT; request ID `trace-214846` logged by both `sensor-api` and `sensor-grpc` (`b_both_transports.txt`). Elasticsearch: `{sensor-api: 3, sensor-grpc: 2}` documents for that ID.

## A2-C. Metrics (2026-10-25)

- Built: `sensor_grpc_*` metrics, `transport` label and `replayed`/`conflict` outcomes, `sensor_rate_limited_total`, DB write Summary → Histogram, Prometheus job `sensor-grpc`, Grafana row "Submit a reading: HTTP vs gRPC" (panels 30–36).
- Targets: `node up · prometheus up · sensor-api up · sensor-grpc up`.
- `scripts/a2_traffic.py --rounds 150` → HTTP `{200:150, 201:225, 409:50, 422:150, 429:30}`, gRPC `{OK:375, INVALID_ARGUMENT:150, ALREADY_EXISTS:50, RESOURCE_EXHAUSTED:30}` (`c_traffic_run.txt`).
- First run, at 16:53:51Z, `[2m]`: HTTP p50 20.8 ms / p95 36.0 ms; gRPC p50 4.5 ms / p95 33.5 ms; DB write p95 32.5 ms (included replay lookups, fixed later).
- Refreshed after the code-review fixes, run 17:13:01–17:15:35Z, evaluated at 17:15:35Z `[2m]`: HTTP p50 11.4 / p95 25.3 ms; gRPC p50 3.7 / p95 9.6 ms; DB write p95 by process: sensor-api 20.1 ms (5.4 writes/s), sensor-grpc 9.5 ms (1.5 writes/s) (`c_prometheus_numbers.txt`). Same client counts as the first run. Grafana panels and Prometheus targets recaptured for this window.
- `.venv/bin/pytest -q` → `109 passed`.
- Code-review fixes (caller reports 4xx as REFUSED; schema tolerates pre-A2 duplicates; one problem type for 500; DB write timed only on real writes; shared rate-limit and field-name helpers; spec reads the limit from settings) → `113 passed`; caller demo and rate-limit demo re-run.
