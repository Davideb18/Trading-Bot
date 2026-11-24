"""Necessary gap-risk bounds; these are not strategy backtest results."""

import json
from pathlib import Path


def shock_loss(notionals, drop, roundtrip_bps=28, fee_roundtrip_bps=11):
    """Conservative friction on pre-shock notional; excludes unknown funding."""
    return sum(notionals) * (drop + (roundtrip_bps + fee_roundtrip_bps) / 10000)


def main():
    rows = []
    for slot in (350, 450, 500, 600):
        for count, drop in ((1, 0.35), (1, 0.50), (2, 0.25)):
            loss = shock_loss([slot] * count, drop)
            rows.append(dict(slot=slot, positions=count, drop=drop, loss=round(loss, 3),
                             one_step_daily_survives_from_flat=loss < 200,
                             two_step_daily_survives_from_flat=loss < 250,
                             one_step_required_unused_daily_headroom=round(loss, 3),
                             two_step_required_unused_daily_headroom=round(loss, 3)))
    out = Path("user_data/forensic/strict_validation/shock_bounds.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dict(
        assumptions="5000 USDT; 1x; 28bps roundtrip slippage; illustrative 11bps roundtrip fees; no prior loss or funding",
        result_type="necessary sizing bounds, not historical or synthetic portfolio replay",
        rows=rows), indent=2), encoding="utf-8")
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
