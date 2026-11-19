from datetime import UTC, datetime
from types import SimpleNamespace

from freqtrade.prop_profile import build_program_profiles, floors
from scripts.prop_backtest_1to1 import compute_hyrotrader_ledger, simulate_funded_withdrawals


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


def test_two_step_phase2_enforces_100_usd_daily_consistency_cap_and_5pct_10pct_floors():
    profs = build_program_profiles(initial_balance=5000.0)
    p2 = profs["two_step_phase2"]
    assert p2["profit_target_percent"] == 0.05
    assert p2["daily_profit_cap_percent"] == 0.40
    assert p2["min_trading_days"] == 4
    fl = floors(p2, 5000.0)
    assert fl["official_daily_floor"] == 4750.0
    assert fl["official_overall_floor"] == 4500.0

    # A single day with $180 profit in Phase 2 is capped at $100 towards the $250 target
    d1 = datetime(2026, 11, 16, tzinfo=UTC)
    orders = [
        dict(idx=0, dt=d1, is_entry=True, filled=3, price=100, cost=300),
        dict(idx=1, dt=d1, is_entry=False, filled=3, price=160, cost=480),
    ]
    rec = dict(orders=orders, open_dt=d1, close_dt=d1, is_short=False,
               open_rate=100, is_open=False, profit_abs=180.0, realized_profit=180.0)
    ledger = compute_hyrotrader_ledger(
        [], [rec], 5000.0, p2["profit_target_percent"], p2["daily_profit_cap_percent"], d1
    )
    assert ledger["target_profit"] == 250.0
    assert ledger["daily_cap"] == 100.0
    assert ledger["counted_profit"] == 100.0
    assert ledger["qualifying_days"] == ["2026-11-16"]


def test_funded_withdrawal_policies_reset_vs_buffer():
    curve = [
        {"date": "2026-01-01", "midnight_equity": 5100.0},
        {"date": "2026-01-02", "midnight_equity": 5260.0},
        {"date": "2026-01-03", "midnight_equity": 5150.0},
        {"date": "2026-01-04", "midnight_equity": 5460.0},
        {"date": "2026-01-05", "midnight_equity": 5720.0},
    ]
    sim = simulate_funded_withdrawals(curve, initial_balance=5000.0, official_max_loss_pct=0.08)
    reset = sim["reset_5250_to_5000"]
    buf = sim["buffer_5450_to_5200"]
    assert reset["payout_count"] == 2
    assert reset["withdrawn_gross_usdt"] == 500.0
    assert reset["withdrawn_net_trader_80pct_usdt"] == 400.0
    assert reset["days_to_first_payout"] == 2
    assert buf["payout_count"] == 2
    assert buf["days_to_first_payout"] == 4
    assert buf["min_account_equity_usdt"] >= reset["min_account_equity_usdt"]


def test_withdrawal_detects_intraday_breach_before_recovery_and_stops():
    curve = [
        {"date": "2026-01-01", "midnight_equity": 5260, "min_day_equity_wick": 5000},
        {"date": "2026-01-02", "midnight_equity": 5250, "min_day_equity_wick": 4840},
        {"date": "2026-01-03", "midnight_equity": 6000, "min_day_equity_wick": 5250},
    ]
    result = simulate_funded_withdrawals(curve)
    reset = result["reset_5250_to_5000"]
    assert result["wick_coverage_complete"]
    assert reset["payout_count"] == 1
    assert not reset["survived_static_floor"]
    assert reset["min_account_equity_usdt"] == 4590
    assert reset["final_account_equity_usdt"] == 5000
    two_step = simulate_funded_withdrawals(curve, official_max_loss_pct=0.10)
    assert two_step["reset_5250_to_5000"]["survived_static_floor"]


def test_continuous_funded_programs_use_distinct_official_floors(monkeypatch):
    from scripts import prop_backtest_1to1 as engine

    observed = []
    def run(bt, cfg, profile, *args, **kwargs):
        observed.append((profile.copy(), kwargs["ignore_target_pause"]))
        return dict(final_equity=5000, net_pnl=0, total_trades=0, halted=False,
                    max_month_dd_close=0, max_month_dd_wick=0)

    monkeypatch.setattr(engine, "_run_continuous_pass", run)
    bt = SimpleNamespace(_prop_policy={}, set_fee=lambda: None)
    profile = build_program_profiles()["one_step_eval"]
    engine._execute_scenario(bt, {}, profile, {}, {}, datetime(2025, 1, 1, tzinfo=UTC),
        datetime(2025, 2, 1, tzinfo=UTC), universe_size=0, name="test", slippage_bps=0,
        strict_wick_guard=True, cohort_step_days=0)
    assert [p["phase_id"] for p, _ in observed] == ["evaluation", "funded", "funded"]
    assert [p["official_daily_limit_percent"] for p, _ in observed] == [0.04, 0.04, 0.05]
    assert [floors(p, 5000)["official_overall_floor"] for p, _ in observed] == [4600, 4600, 4500]
    assert bt._prop_profile == profile
