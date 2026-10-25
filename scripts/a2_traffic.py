"""Repeatable success and failure traffic over HTTP and gRPC, to populate the
"Submit a reading: HTTP vs gRPC" Grafana row.

    .venv/bin/python scripts/a2_traffic.py --rounds 90      # about 90 s

Every round sends, over each transport: a valid reading, the same reading again
(idempotent replay), an invalid one (humidity 150) and, every 3rd round, a conflicting
one (same device and timestamp, different temperature). Every 10th round adds a burst
of 7 readings for one device on each transport, to trigger the rate limiter
(429 / RESOURCE_EXHAUSTED). Devices rotate (dev-920…929 over HTTP, dev-930…939 over gRPC) so the normal traffic
stays under the per-device limit. Deterministic: the same counts every run.
"""

import argparse
import logging
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import grpc  # noqa: E402
import httpx  # noqa: E402

from app.grpc_gen import sensor_ingest_pb2 as pb  # noqa: E402
from app.grpc_gen import sensor_ingest_pb2_grpc as pb_grpc  # noqa: E402

log = logging.getLogger("a2_traffic")


def body(device: str, **overrides) -> dict:
    b = {"device_id": device, "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
         "temperature_c": 22.5, "humidity_pct": 45.0, "battery_pct": 88.0}
    b.update(overrides)
    return b


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--target", default="localhost:50051")
    p.add_argument("--rounds", type=int, default=90, help="one round per second")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    http_codes: Counter = Counter()
    grpc_codes: Counter = Counter()
    http = httpx.Client(base_url=args.url, timeout=5.0)
    channel = grpc.insecure_channel(args.target)
    stub = pb_grpc.IngestServiceStub(channel)

    def via_http(b: dict, tag: str) -> None:
        r = http.post("/readings", json=b, headers={"X-Request-ID": f"traffic-http-{tag}"})
        http_codes[r.status_code] += 1

    def via_grpc(b: dict, tag: str) -> None:
        try:
            stub.SubmitReading(pb.SubmitReadingRequest(**b), timeout=2.0, metadata=(("x-request-id", f"traffic-grpc-{tag}"),))
            grpc_codes["OK"] += 1
        except grpc.RpcError as err:
            grpc_codes[err.code().name] += 1

    start = time.monotonic()
    for i in range(args.rounds):
        for send, base in ((via_http, 920), (via_grpc, 930)):
            device = f"dev-{base + i % 10:03d}"
            valid = body(device)
            send(valid, f"{i}-ok")
            send(valid, f"{i}-replay")
            send(body(device, humidity_pct=150), f"{i}-invalid")
            if i % 3 == 0:
                send({**valid, "temperature_c": 30.0}, f"{i}-conflict")
            if i % 10 == 9:
                for k in range(7):
                    send(body("dev-919" if send is via_http else "dev-949"), f"{i}-burst{k}")
        time.sleep(max(0.0, start + i + 1 - time.monotonic()))
        if i % 15 == 14:
            log.info("round %d: HTTP %s · gRPC %s", i + 1, dict(sorted(http_codes.items())), dict(sorted(grpc_codes.items())))
    log.info("done: HTTP %s · gRPC %s", dict(sorted(http_codes.items())), dict(sorted(grpc_codes.items())))
    channel.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
