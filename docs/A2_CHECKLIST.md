# A2 requirement checklist

Every bullet of the brief, with where its evidence is. `[x]` means done and verified.

## Part A: HTTP API (40)
- [x] REST table: 6 constraints, code on demand optional, meaning + example (report A.1; **author: check against lecture §7.4**)
- [x] Review all endpoints: paths, methods, validation, status codes, consistent error format
- [x] OpenAPI: 2–3 sentence explanation, spec file matching routes/inputs/responses, used to inspect/test one endpoint
- [x] Idempotency for a retried operation
- [x] Rate limiting on one endpoint: accepted requests + `429` with `Retry-After`; state the limit, who shares it, when calls are allowed again
- [x] Before/after table for the user task; list other endpoints changed/left alone
- [x] Update affected callers (dashboard, simulator, load script)
- [x] Tests: valid, invalid, repeated request
- [x] Python caller: several calls in one run, timeout, limited retry with backoff, circuit breaker
- [x] Caller output showing success, retry, breaker open (blocked), recovery; settings stated

## Part B: gRPC (30)
- [x] One operation as a gRPC service in its own container; HTTP kept; same rules and DB (not a wrapper)
- [x] `.proto`, generation command, handler
- [x] Python client: input, deadline, reports results/errors
- [x] One success + one failure through both HTTP and gRPC; statuses explained
- [x] Call-path diagram for both, showing the generated stub and the server handler
- [x] Logs + request-ID correlation across the gRPC boundary

## Part C: metrics (30)
- [x] Audit of A1 metrics: useful / missing / fix
- [x] Prometheus scrapes the gRPC service
- [x] Grafana: calls + error rate by RPC method and status, beside HTTP
- [x] Duration histogram, p50 and p95 over a stated window
- [x] In-flight (or message size), with the choice explained
- [x] Another missing metric (or why none)
- [x] Repeatable success/failure traffic
- [x] Per metric: purpose, type, unit, labels, code location, query; client vs server stated
- [x] Bounded labels only

## Submit
- [x] Code, OpenAPI document, `.proto`, generation command, Python client, tests, Prometheus config, Grafana dashboard
- [x] README with run and test commands
- [x] Report: REST table, OpenAPI explanation + use, before/after, rate-limit + reliability results, call-path diagram, HTTP/gRPC examples, metric gap table, queries, dashboard evidence
- [x] Cite reused work and AI assistance (the gRPC workshop was not used, so it isn't cited)
