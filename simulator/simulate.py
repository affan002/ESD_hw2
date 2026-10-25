"""Device fleet simulator: pushes readings to POST /readings at a steady rate.

Run with `python -m simulator.simulate`. The fleet is deterministic for a given
seed. The last four devices misbehave on purpose (see PROFILES_FAULTY).
"""

import argparse
import logging
import math
import os
import random
import signal
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx

from app.logging_setup import request_id_var, setup_logging

log = logging.getLogger("simulator")

# Assigned to the last N devices, in this order.
PROFILES_FAULTY = ("overheat", "low_battery", "invalid", "dropout")
INVALID_KINDS = ("humidity_out_of_range", "missing_temperature", "future_timestamp")

OVERHEAT_STEP_C = 0.6  # temperature climb per reading
OVERHEAT_RESET_C = 45.0  # back to baseline once above this
LOW_BATTERY_START = 20.0
LOW_BATTERY_DRAIN = 0.25  # per reading
NORMAL_BATTERY_DRAIN = 0.002  # per reading
INVALID_PROBABILITY = 0.3
DROPOUT_PERIOD_S = 120.0  # online for the first half of each period, silent for the second
SUMMARY_EVERY_S = 30.0


def assign_profiles(n: int) -> list[str]:
    profiles = ["normal"] * n
    for i, profile in enumerate(PROFILES_FAULTY):
        idx = n - len(PROFILES_FAULTY) + i
        if idx >= 0:
            profiles[idx] = profile
    return profiles


class Device:
    def __init__(self, index: int, profile: str, seed: int):
        self.device_id = f"dev-{index + 1:03d}"
        self.profile = profile
        self.rng = random.Random(seed + index)
        self.base_temp = self.rng.uniform(19.0, 25.0)
        self.base_humidity = self.rng.uniform(35.0, 60.0)
        self.battery = LOW_BATTERY_START if profile == "low_battery" else self.rng.uniform(60.0, 100.0)
        self.overheat_step = 0
        self.invalid_count = 0

    def is_online(self, elapsed_s: float) -> bool:
        if self.profile == "dropout":
            return (elapsed_s % DROPOUT_PERIOD_S) < DROPOUT_PERIOD_S / 2
        return True

    def next_payload(self, now: datetime, elapsed_s: float) -> tuple[dict, str | None]:
        """Build the next reading. Returns (payload, invalid_kind or None)."""
        rng = self.rng
        if self.profile == "overheat":
            temp = self.base_temp + OVERHEAT_STEP_C * self.overheat_step
            self.overheat_step = 0 if temp > OVERHEAT_RESET_C else self.overheat_step + 1
        else:
            temp = self.base_temp + 1.5 * math.sin(2 * math.pi * elapsed_s / 600) + rng.gauss(0, 0.3)
        humidity = min(100.0, max(0.0, self.base_humidity + rng.gauss(0, 1.0)))
        drain = LOW_BATTERY_DRAIN if self.profile == "low_battery" else NORMAL_BATTERY_DRAIN
        self.battery = max(1.0, self.battery - drain)

        payload = {
            "device_id": self.device_id,
            "timestamp": now.isoformat(),
            "temperature_c": round(temp, 2),
            "humidity_pct": round(humidity, 2),
            "battery_pct": round(self.battery, 2),
        }

        invalid_kind = None
        if self.profile == "invalid" and rng.random() < INVALID_PROBABILITY:
            invalid_kind = INVALID_KINDS[self.invalid_count % len(INVALID_KINDS)]
            self.invalid_count += 1
            if invalid_kind == "humidity_out_of_range":
                payload["humidity_pct"] = 150.0
            elif invalid_kind == "missing_temperature":
                del payload["temperature_c"]
            else:
                payload["timestamp"] = (now + timedelta(hours=1)).isoformat()
        return payload, invalid_kind


def build_fleet(n: int, seed: int) -> list[Device]:
    return [Device(i, profile, seed) for i, profile in enumerate(assign_profiles(n))]


def wait_for_api(client: httpx.Client, timeout_s: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if client.get("/health").status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1.0)
    return False


def send(client: httpx.Client, device: Device, elapsed_s: float, stats: Counter) -> None:
    payload, invalid_kind = device.next_payload(datetime.now(timezone.utc), elapsed_s)
    request_id = f"sim-{uuid.uuid4().hex[:12]}"
    token = request_id_var.set(request_id)
    try:
        stats["sent"] += 1
        try:
            resp = client.post("/readings", json=payload, headers={"X-Request-ID": request_id})
        except httpx.HTTPError as exc:
            stats["conn_errors"] += 1
            log.warning(
                "request failed: %s",
                type(exc).__name__,
                extra={"event": "sim_send_failed", "device_id": device.device_id, "error": type(exc).__name__},
            )
            return
        code = resp.status_code
        if code in (200, 201):  # 200 = an identical reading was already stored (idempotent replay)
            stats[f"status_{code}"] += 1
            return
        stats[{422: "status_422", 429: "status_429"}.get(code, "status_5xx" if code >= 500 else "status_other")] += 1
        extra = {"event": "sim_send_failed", "device_id": device.device_id, "status_code": code}
        if invalid_kind:
            extra["fault"] = invalid_kind
        log.warning("reading not accepted: HTTP %d", code, extra=extra)
    finally:
        request_id_var.reset(token)


def log_summary(stats: Counter, final: bool = False) -> None:
    keys = ("sent", "status_201", "status_200", "status_422", "status_429", "status_5xx", "status_other", "conn_errors")
    log.info(
        "simulator final summary" if final else "simulator summary",
        extra={"event": "sim_summary", "stats": {k: stats[k] for k in keys}},
    )


def run(api_url: str, n_devices: int, interval_s: float, seed: int, duration_s: float) -> int:
    fleet = build_fleet(n_devices, seed)
    log.info(
        "simulator starting",
        extra={
            "event": "sim_startup",
            "config": {
                "api_url": api_url,
                "devices": n_devices,
                "interval_s": interval_s,
                "seed": seed,
                "duration_s": duration_s,
                "profiles": {d.device_id: d.profile for d in fleet if d.profile != "normal"},
            },
        },
    )

    stopping = False

    def stop(signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    stats: Counter = Counter()
    with httpx.Client(base_url=api_url, timeout=5.0) as client:
        if not wait_for_api(client):
            log.error("API not healthy after 30s, giving up", extra={"event": "sim_api_unavailable"})
            return 1

        tick_s = interval_s / n_devices
        start = time.monotonic()
        next_tick = start
        last_summary = start
        k = 0
        while not stopping:
            now = time.monotonic()
            elapsed = now - start
            if duration_s and elapsed >= duration_s:
                break
            device = fleet[k % n_devices]
            k += 1
            if device.is_online(elapsed):
                send(client, device, elapsed, stats)
            if now - last_summary >= SUMMARY_EVERY_S:
                log_summary(stats)
                last_summary = now
            next_tick += tick_s
            time.sleep(max(0.0, next_tick - time.monotonic()))

    log_summary(stats, final=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Simulate a fleet of IoT sensors.")
    parser.add_argument("--api-url", default=os.getenv("API_URL", "http://localhost:8000"))
    parser.add_argument("--devices", type=int, default=int(os.getenv("SIM_DEVICES", "20")))
    parser.add_argument("--interval", type=float, default=float(os.getenv("SIM_INTERVAL_S", "5")),
                        help="seconds between readings from the same device")
    parser.add_argument("--seed", type=int, default=int(os.getenv("SIM_SEED", "42")))
    parser.add_argument("--duration", type=float, default=float(os.getenv("SIM_DURATION_S", "0")),
                        help="stop after this many seconds (0 = run forever)")
    args = parser.parse_args(argv)
    if args.devices < 1 or args.interval <= 0:
        parser.error("--devices must be >= 1 and --interval > 0")

    setup_logging("simulator", os.getenv("LOG_LEVEL", "INFO").upper())
    return run(args.api_url, args.devices, args.interval, args.seed, args.duration)


if __name__ == "__main__":
    sys.exit(main())
