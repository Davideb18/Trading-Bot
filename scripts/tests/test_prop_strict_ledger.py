from datetime import UTC, datetime

from scripts.prop_backtest_1to1 import compute_hyrotrader_ledger


def test_closed_trade_combines_partial_exits_and_uses_gross_before_fees():
    first = datetime(2026, 11, 16, tzinfo=UTC)
    partial = datetime(2026, 11, 17, tzinfo=UTC)
    final = datetime(2026, 11, 18, tzinfo=UTC)
    orders = [
        dict(idx=0, dt=first, is_entry=True, filled=3, price=100, cost=300),
        dict(idx=1, dt=partial, is_entry=False, filled=1, price=101, cost=101),
        dict(idx=2, dt=final, is_entry=False, filled=2, price=101, cost=202),
    ]
    record = dict(orders=orders, open_dt=first, close_dt=final, is_short=False,
                  open_rate=100, is_open=False, profit_abs=2.67, realized_profit=2.67)
    result = compute_hyrotrader_ledger([], [record], 5000, 0.1, 0.4, first)
    assert result["qualifying_days"] == ["2026-11-18"]
    assert abs(sum(result["day_profits"].values()) - 2.67) < 1e-9
    record["is_open"] = True
    assert compute_hyrotrader_ledger([], [record], 5000, 0.1, 0.4, first)["qualifying_days"] == []
