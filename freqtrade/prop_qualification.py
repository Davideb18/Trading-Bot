"""Strict qualifying-day evidence shared by live supervision and research."""

import math
from datetime import UTC


def qualifying_close_day(*, is_open, close_dt, remaining_qty, exit_events, initial_balance):
    """Count a fully closed trade using aggregate entry notional and gross execution PnL.

    Missing execution evidence cannot be replaced with net profit ratios or margin.
    Positive and negative gross results qualify under the published ±1% rule.
    """
    if is_open or close_dt is None or close_dt.tzinfo is None or not exit_events:
        return None
    if not math.isfinite(remaining_qty) or abs(remaining_qty) > 1e-12:
        return None
    notional = sum(e["entry_notional"] for e in exit_events)
    gross = sum(e["gross_pnl"] for e in exit_events)
    if not all(math.isfinite(v) for v in (notional, gross, initial_balance)):
        return None
    if initial_balance <= 0 or notional <= 0:
        return None
    if notional >= initial_balance * 0.05 and abs(gross) >= notional * 0.01:
        return close_dt.astimezone(UTC).strftime("%Y-%m-%d")
    return None
