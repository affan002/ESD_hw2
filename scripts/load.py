#!/usr/bin/env python3
"""Steady, repeatable load for experiment E1: POST /readings at a fixed rate.

Open loop: a request is started every 1/rate seconds whatever the server's
latency, so a slow server can't lower the offered load (a thread pool
absorbs slow requests). Every request is a normal reading for one device and
has X-Request-ID load-<stage>-NNNNN, so each stage can be found in Kibana.

Prints one JSON summary line (client-side view). Standard library only.

    python3 scripts/load.py --stage baseline --rate 5 --duration 150
"""

import argparse
import json
import math
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone


def utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def percentile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank percentile of an already sorted list."""
    return sorted_values[max(0, math.ceil(q * len(sorted_values)) - 1)]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--rate", type=float, default=5.0, help="requests per second")
    p.add_argument("--duration", type=float, default=150.0, help="seconds")
    p.add_argument("--stage", default="run", help="used in request ids: load-<stage>-NNNNN")
    p.add_argument("--device", default="dev-100", help="first device id; see --devices")
    p.add_argument(
        "--devices", type=int, default=20,
        help="spread requests round-robin over this many devices (dev-100, dev-101, …). The API rate-limits "
             "each device to a burst of 5 then 1/s, so keep rate/devices <= 1 to avoid 429s",
    )
    p.add_argument("--timeout", type=float, default=5.0, help="client timeout per request, seconds")
    p.add_argument("--out", help="also write the JSON summary to this file")
    args = p.parse_args()

    first_device = int(args.device.removeprefix("dev-"))
    if args.devices < 1 or first_device + args.devices - 1 > 999:
        p.error(f"--device {args.device} with --devices {args.devices} goes past dev-999 (device ids have 3 digits)")
    results: list[tuple[int, float]] = []
    lock = threading.Lock()

    def send(n: int) -> None:
        body = json.dumps({
            "device_id": f"dev-{first_device + n % args.devices:03d}",
            "timestamp": utc(time.time()),
            "temperature_c": 22.0,
            "humidity_pct": 45.0,
            "battery_pct": 88.0,
        }).encode()
        req = urllib.request.Request(
            f"{args.url}/readings", data=body, method="POST",
            headers={"Content-Type": "application/json", "X-Request-ID": f"load-{args.stage}-{n:05d}"},
        )
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=args.timeout) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except Exception:  # timeout or connection error
            status = 0
        with lock:
            results.append((status, time.perf_counter() - t0))

    start_wall, start = time.time(), time.monotonic()
    sent = 0
    with ThreadPoolExecutor(max_workers=32) as pool:
        while True:
            due = start + sent / args.rate
            if due - start >= args.duration:
                break
            delay = due - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            pool.submit(send, sent)
            sent += 1
        end_wall = time.time()
    # (leaving the pool waits for the last requests to finish)

    latencies = sorted(dt * 1000 for _, dt in results)
    codes: dict[str, int] = {}
    for status, _ in results:
        codes[str(status)] = codes.get(str(status), 0) + 1
    summary = {
        "stage": args.stage,
        "start_utc": utc(start_wall),
        "end_utc": utc(end_wall),
        "rate_per_s": args.rate,
        "sent": sent,
        "status_counts": codes,
        "client_latency_ms": {
            "p50": round(percentile(latencies, 0.50), 1),
            "p95": round(percentile(latencies, 0.95), 1),
            "p99": round(percentile(latencies, 0.99), 1),
            "max": round(latencies[-1], 1),
            "mean": round(sum(latencies) / len(latencies), 1),
        },
    }
    line = json.dumps(summary)
    print(line)
    if args.out:
        with open(args.out, "w") as f:
            f.write(line + "\n")
    return 0 if codes.get("201", 0) == sent else 1


if __name__ == "__main__":
    raise SystemExit(main())
