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


def test_funded_withdrawal_includes_wide_buffer_5650_to_5400():
    curve = [
        {"date": "2026-01-01", "midnight_equity": 5260.0, "min_day_equity_wick": 5000.0},
        {"date": "2026-01-02", "midnight_equity": 5660.0, "min_day_equity_wick": 5200.0},
        {"date": "2026-01-03", "midnight_equity": 5200.0, "min_day_equity_wick": 5050.0},
    ]
    sim = simulate_funded_withdrawals(curve, initial_balance=5000.0, official_max_loss_pct=0.08)
    wide = sim["buffer_5650_to_5400"]
    assert wide["trigger_equity_usdt"] == 5650.0
    assert wide["post_withdrawal_base_usdt"] == 5400.0
    assert wide["payout_count"] == 1
    assert wide["survived_static_floor"] is True
    assert wide["min_account_equity_usdt"] >= sim["reset_5250_to_5000"]["min_account_equity_usdt"]


def test_default_money_grid_and_apply_candidate_controls():
    from scripts.prop_backtest_1to1 import DEFAULT_MONEY_GRID, _apply_candidate_money_params

    ids = [c["id"] for c in DEFAULT_MONEY_GRID]
    assert len(ids) == len(set(ids))
    assert len(ids) >= 24
    # All candidates must stay strictly under the $1,250 (25%) Funded margin limit at 1x leverage
    for c in DEFAULT_MONEY_GRID:
        assert c["max_aggregate_notional_usdt"] <= 1200.0
        assert c["max_slot_notional_usdt"] <= c["max_aggregate_notional_usdt"]

    bt = SimpleNamespace(
        _prop_profile={"initial_balance": 5000.0},
        _prop_policy={},
        config={},
        strategy=SimpleNamespace(),
        wallets=SimpleNamespace(_config={}),
        _macro_enabled=True,
        _macro_1h_trigger=-4.0,
        _macro_24h_trigger=-8.0,
        _macro_1h_recover=-2.5,
        _macro_24h_recover=-6.0,
        _dynamic_shield_enabled=False,
    )
    cfg, profile = {}, {}

    shield_cand = next(c for c in DEFAULT_MONEY_GRID if c["id"] == "S3_E275_SLOT500_AGG1200_SHIELD")
    _apply_candidate_money_params(bt, cfg, profile, shield_cand)
    assert bt._dynamic_shield_enabled is True
    assert bt.config["amend_last_stake_amount"] is False
    assert bt._prop_policy["max_aggregate_notional_usdt"] == 1200.0

    macro_off = next(c for c in DEFAULT_MONEY_GRID if c["id"] == "S3_E255_SLOT400_AGG950_MACRO_OFF")
    _apply_candidate_money_params(bt, cfg, profile, macro_off)
    assert bt._macro_enabled is False
    assert bt._dynamic_shield_enabled is False

    tight_hr = next(c for c in DEFAULT_MONEY_GRID if c["id"] == "S3_E255_SLOT400_AGG950_TIGHT_HEADROOM")
    _apply_candidate_money_params(bt, cfg, profile, tight_hr)
    assert bt._prop_policy["latency_gap_stress_bps"] == 100.0
    assert bt._prop_policy["min_headroom_buffer_usdt"] == 15.0

    unlim = next(c for c in DEFAULT_MONEY_GRID if c["id"] == "S3_UNLIM_SLOT500_AGG950")
    _apply_candidate_money_params(bt, cfg, profile, unlim)
    assert bt.config["amend_last_stake_amount"] is True


def test_delisted_market_and_tier_in_memory_fallback(tmp_path):
    from scripts.prop_backtest_1to1 import PropParityBacktesting

    fake_bt = SimpleNamespace(
        exchange=SimpleNamespace(
            markets={"BTC/USDT:USDT": {"symbol": "BTC/USDT:USDT", "linear": True, "quote": "USDT", "contract": True, "swap": True}},
            _leverage_tiers={"BTC/USDT:USDT": [{"tier": 1, "maxLeverage": 50.0}]},
            _api=SimpleNamespace(markets={}),
        ),
        config={"datadir": tmp_path, "stake_currency": "USDT"},
        prop_synthetic_market_pairs=[],
        prop_synthetic_leverage_tier_pairs=[],
    )
    PropParityBacktesting._ensure_delisted_markets(fake_bt, ["BTC/USDT:USDT", "OM/USDT:USDT"])
    PropParityBacktesting._ensure_leverage_tiers(fake_bt, ["BTC/USDT:USDT", "OM/USDT:USDT"])
    assert "OM/USDT:USDT" in fake_bt.exchange.markets
    assert fake_bt.exchange.markets["OM/USDT:USDT"]["id"] == "OMUSDT"
    assert fake_bt.prop_synthetic_market_pairs == ["OM/USDT:USDT"]
    assert "OM/USDT:USDT" in fake_bt.exchange._leverage_tiers
    assert "maintAmt" in fake_bt.exchange._leverage_tiers["OM/USDT:USDT"][0]
    assert fake_bt.prop_synthetic_leverage_tier_pairs == ["OM/USDT:USDT"]


def test_btc_macro_respects_disabled_and_custom_thresholds():
    from scripts.prop_backtest_1to1 import PropParityBacktesting

    ts = int(datetime(2025, 4, 13, 12, 0, tzinfo=UTC).timestamp())
    # -3.5% drop in 1h: triggers tight (-3.0%), does NOT trigger standard (-4.0%), never triggers when disabled
    btc_map = {
        ((ts - 1) // 300) * 300: 96.5,
        ((ts - 3600 - 1) // 300) * 300: 100.0,
        ((ts - 86400 - 1) // 300) * 300: 100.0,
    }
    bt = SimpleNamespace(
        _btc_close_map=btc_map,
        _btc_lookup=lambda t: PropParityBacktesting._btc_lookup(SimpleNamespace(_btc_close_map=btc_map), t),
        _macro_enabled=True,
        _macro_1h_trigger=-4.0,
        _macro_24h_trigger=-8.0,
        _macro_1h_recover=-2.5,
        _macro_24h_recover=-6.0,
        prop_macro_paused=False,
        prop_macro_clean_streak=0,
    )
    PropParityBacktesting._update_btc_macro(bt, datetime.fromtimestamp(ts, tz=UTC))
    assert bt.prop_macro_paused is False

    bt._macro_1h_trigger = -3.0
    PropParityBacktesting._update_btc_macro(bt, datetime.fromtimestamp(ts, tz=UTC))
    assert bt.prop_macro_paused is True

    bt._macro_enabled = False
    PropParityBacktesting._update_btc_macro(bt, datetime.fromtimestamp(ts, tz=UTC))
    assert bt.prop_macro_paused is False


def test_funded_ledger_has_no_daily_consistency_cap_or_pause():
    import math

    d1 = datetime(2026, 11, 16, tzinfo=UTC)
    orders = [
        dict(idx=0, dt=d1, is_entry=True, filled=3, price=100, cost=300),
        dict(idx=1, dt=d1, is_entry=False, filled=3, price=200, cost=600),
    ]
    rec = dict(orders=orders, open_dt=d1, close_dt=d1, is_short=False,
               open_rate=100, is_open=False, profit_abs=300.0, realized_profit=300.0)
    profs = build_program_profiles(initial_balance=5000.0)
    f1 = profs["one_step_funded"]
    ledger = compute_hyrotrader_ledger(
        [], [rec], 5000.0, f1["profit_target_percent"], f1["daily_profit_cap_percent"], d1
    )
    assert ledger["counted_profit"] == 300.0
    assert math.isinf(ledger["daily_pause_threshold"])
