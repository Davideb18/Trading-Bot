"""Bound positive adjustments without suppressing risk-reducing exits."""

import math


def clamp_addition(proposed, entry_stakes, *, max_additions=2, min_stake=None):
    if proposed is None or proposed <= 0:
        return proposed
    if (type(max_additions) is not int or not 0 <= max_additions <= 3
            or not entry_stakes or len(entry_stakes) - 1 >= max_additions
            or not math.isfinite(proposed)
            or any(not math.isfinite(s) or s <= 0 for s in entry_stakes)):
        return None
    # Every addition is no larger than any earlier fill, including the first entry.
    stake = min(proposed, *entry_stakes)
    return None if min_stake is not None and stake < min_stake else stake
