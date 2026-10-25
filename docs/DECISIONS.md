# Design decisions (Assignment 2)

One entry per decision: what was chosen, why, and the alternative that was rejected. The report's reasoning is drawn from here. Decided 2026-10-25.

## Part A: HTTP API

| # | Decision | Choice | Why | Rejected alternative |
|---|---|---|---|---|
| A-1 | User task | **A device submits a reading** (`POST /readings`) | It's the operation that writes data and that a device retries after a timeout, so idempotency, rate limiting and the reliable caller all apply to it naturally | `GET /devices/{id}`: it's safe to repeat already, so there's nothing to protect |
| A-2 | Error format | **RFC 9457 `application/problem+json`** on every error: `{type, title, status, detail, instance, request_id, errors?}` | One shape for 404/409/422/429/500, so a caller branches on `status`/`type` without parsing text. `request_id` links an error to its log lines | Keeping FastAPI's default: `detail` is a string for 404 but a list for 422 |
| A-3 | Idempotency | **Natural key `(device_id, timestamp)`**: an exact repeat returns `200` and the stored reading (`Idempotent-Replayed: true`); the same key with different values returns `409` | A device can't take two readings at the same instant, so the payload already identifies itself; no extra header or key store is needed | An `Idempotency-Key` header: callers must generate and keep a key, and the server must store keys and expire them |
| A-4 | Rate limit | **`POST /readings`, per `device_id`, token bucket: burst 5, refill 1/s.** Over the limit → `429` + `Retry-After` (seconds until the next token) | Behind Docker every caller has the same IP, so per-IP would make the whole fleet share one limit. Per device isolates one misbehaving device. The simulator sends 1 per 5 s per device, far under it | Per client IP (unfair here); global (one device could starve all); fixed window (allows 2× bursts at window edges) |
| A-5 | Caller reliability | **Timeout 2 s · 3 attempts with backoff 0.2/0.4/0.8 s + jitter, only on timeout/connection error/5xx · breaker opens after 3 consecutive failed attempts, half-opens after 10 s** | Normal p99 is about 25 ms, so 2 s only fires on a real problem. Retrying a POST is safe only because of A-3. 4xx is never retried (repeating can't fix it) | Retrying everything (would retry 422s forever); no breaker (keeps hammering a sick server) |
| A-6 | Other endpoints | Validate the `device_id` path param (422 instead of a misleading 404); add `GET /readings/{id}` and a `Location` header on 201; leave `/devices`, `/anomalies` and `/admin/faults` alone | Bad input should be told apart from "not found"; a created resource should be addressable | Renaming `/anomalies` to `/readings?anomalous=true`: would break the dashboard for no real gain |

## Part B: gRPC

| # | Decision | Choice | Why | Rejected alternative |
|---|---|---|---|---|
| B-1 | Operation | Unary `SubmitReading` | It's the chosen task; devices send one reading at a time | Client streaming: no batching need |
| B-2 | Shared rules | Both transports call `app/ingest.py: submit_reading()`; the gRPC server is its own container on the same SQLite volume | Same business rules and same source of truth, not a wrapper around HTTP | gRPC calling the HTTP endpoint (forbidden by the brief, and adds a hop) |
| B-3 | Status mapping | 201/200 → OK · 422 → INVALID_ARGUMENT · 409 → ALREADY_EXISTS · 429 → RESOURCE_EXHAUSTED · 500 → INTERNAL · client timeout → DEADLINE_EXCEEDED | Standard gRPC code meanings | — |
| B-4 | Correlation | `x-request-id` metadata, validated with the same regex as HTTP, set in a server interceptor, echoed back | Same mechanism as A1's `X-Request-ID`, so one ID can be searched across both services | W3C `traceparent`: there's no tracing backend to use it |

## Part C: metrics

| # | Decision | Choice | Why | Rejected alternative |
|---|---|---|---|---|
| C-1 | gRPC metrics | `sensor_grpc_requests_total{method,code}`, `sensor_grpc_request_duration_seconds{method}`, `sensor_grpc_requests_in_progress{method}` | RED metrics for the new service, with bounded labels | — |
| C-2 | Duration measured at | **Server** (interceptor), with the same buckets as the HTTP histogram | Comparable with HTTP, which is also measured server-side; the caller has no metrics endpoint | Client side: includes the network, but the caller isn't scraped |
| C-3 | In-flight vs message size | **In-flight** | The messages are a constant ~100 B, so size carries no signal; in-flight shows a pile-up under delay | Message size |
| C-4 | Other missing metrics | `sensor_rate_limited_total{route}`; outcomes `replayed` and `conflict` and a `transport` label on `sensor_readings_total`; DB write Summary → Histogram | Today you can't see throttling or retries, nor compare HTTP and gRPC outcomes; a Summary can't be aggregated across two processes | None needed (false: these questions are unanswerable today) |
| C-5 | Percentile window | `[1m]` | ≥ 4× the 5 s scrape, matching A1's panels | `[5m]`: too slow for a short demo |
