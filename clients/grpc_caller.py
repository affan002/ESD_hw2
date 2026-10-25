"""Call the gRPC SubmitReading RPC directly (no browser, no HTTP).

    .venv/bin/python -m clients.grpc_caller --device dev-903 --temperature 22.5 --humidity 45 --battery 88
    .venv/bin/python -m clients.grpc_caller --device dev-903 --humidity 150          # INVALID_ARGUMENT
    .venv/bin/python -m clients.grpc_caller --device dev-903 --timestamp 2026-10-25T10:00:00Z --repeat 2

It sets a deadline on every call (default 2 s; gRPC then fails it with DEADLINE_EXCEEDED),
sends `x-request-id: cli-<12 hex>` as metadata (or --request-id), and reports either the
stored reading or the gRPC status code and details, plus the request ID the server echoed.
"""

import argparse
import logging
import sys
import uuid
from datetime import datetime, timezone

import grpc

from app.grpc_gen import sensor_ingest_pb2 as pb
from app.grpc_gen import sensor_ingest_pb2_grpc as pb_grpc

log = logging.getLogger("grpc_caller")


def submit(stub: pb_grpc.IngestServiceStub, request: pb.SubmitReadingRequest, deadline_s: float, request_id: str) -> bool:
    try:
        response, call = stub.SubmitReading.with_call(
            request, timeout=deadline_s, metadata=(("x-request-id", request_id),)
        )
    except grpc.RpcError as err:
        echoed = dict(err.initial_metadata() or ()).get("x-request-id", "-")
        log.info("FAILED  %s: %s  [request_id=%s]", err.code().name, err.details(), echoed)
        return False
    echoed = dict(call.initial_metadata()).get("x-request-id", "-")
    r = response.reading
    log.info("OK  reading %d%s  device=%s timestamp=%s anomaly=%s %s  [request_id=%s]",
             r.id, " (replayed)" if response.replayed else "", r.device_id, r.timestamp,
             r.is_anomaly, list(r.anomaly_reasons), echoed)
    return True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--target", default="localhost:50051")
    p.add_argument("--device", default="dev-903")
    p.add_argument("--timestamp", help="ISO-8601 with timezone (default: now)")
    p.add_argument("--temperature", type=float, default=22.5)
    p.add_argument("--humidity", type=float, default=45.0)
    p.add_argument("--battery", type=float, default=88.0)
    p.add_argument("--omit", choices=("temperature_c", "humidity_pct", "battery_pct"), help="leave a field out")
    p.add_argument("--deadline", type=float, default=2.0, help="seconds")
    p.add_argument("--request-id", help="default: cli-<12 hex>")
    p.add_argument("--repeat", type=int, default=1, help="send the same request N times (shows idempotency)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)

    fields = {"temperature_c": args.temperature, "humidity_pct": args.humidity, "battery_pct": args.battery}
    if args.omit:
        fields.pop(args.omit)
    request = pb.SubmitReadingRequest(
        device_id=args.device,
        timestamp=args.timestamp or datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        **fields,
    )
    request_id = args.request_id or f"cli-{uuid.uuid4().hex[:12]}"
    with grpc.insecure_channel(args.target) as channel:
        stub = pb_grpc.IngestServiceStub(channel)  # the generated client stub
        ok = all([submit(stub, request, args.deadline, request_id) for _ in range(args.repeat)])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
