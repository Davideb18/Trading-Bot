"""Bounded read intervals, not a claim of atomic or instantaneous exchange state."""
import math
import time

MAX_AGE_MS = 15000
MAX_SKEW_MS = 2000


def milliseconds():
    return time.time() * 1000


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_reads(reads, now=None, required=()):
    now = milliseconds() if now is None else now
    if not isinstance(reads, list) or not reads:
        raise ValueError("Missing component read timestamps")
    for read in reads:
        start, end, server, elapsed = (read[k] for k in (
            "started_ms", "received_ms", "server_ms", "elapsed_ms"))
        if not all(finite(v) for v in (start, end, server, elapsed)):
            raise ValueError("Invalid component timestamp")
        if (not 0 <= end - start <= MAX_AGE_MS or not 0 <= elapsed <= MAX_AGE_MS
                or abs(end - start - elapsed) > MAX_SKEW_MS
                or not 0 <= now - start <= MAX_AGE_MS or end > now
                or not -MAX_SKEW_MS <= end - server <= MAX_AGE_MS
                or server < start - MAX_SKEW_MS
                or not -MAX_SKEW_MS <= now - server <= MAX_AGE_MS):
            raise ValueError("Stale or discordant component read")
    if max(r["received_ms"] for r in reads) - min(r["started_ms"] for r in reads) > MAX_AGE_MS:
        raise ValueError("Incoherent snapshot read window")
    if not set(required).issubset({r["component"] for r in reads}):
        raise ValueError("Missing essential snapshot component")


def read_record(component, start, monotonic_start, response):
    end = milliseconds()
    record = dict(component=component, started_ms=start, received_ms=end,
                  server_ms=response.get("time"),
                  elapsed_ms=(time.monotonic() - monotonic_start) * 1000)
    validate_reads([record], now=end)
    return record


def validate_snapshot(snapshot, now=None):
    required = ["open_orders", "positions", "open_orders_end", "positions_end"]
    for cid in snapshot["orders"]:
        required.extend(("realtime:" + cid, "history:" + cid, "executions:" + cid))
    validate_reads(snapshot.get("reads"), now, required)


def validate_risk_reads(snapshot, state, now=None):
    now = milliseconds() if now is None else now
    validate_snapshot(snapshot, now)
    # These must come from actual remote calls, not state-file save time.
    reads = snapshot["reads"] + state.get("risk_component_reads", [])
    validate_reads(reads, now, ("identity", "account_mode", "equity"))
    for position in snapshot["positions"]:
        if position["amount"]:
            # Position updatedTime is NOT a mark-price observation timestamp.
            observed = position.get("mark_price_observed_ms")
            if not finite(observed) or not 0 <= now - observed <= MAX_AGE_MS:
                raise ValueError("Unverified mark price observation time")
