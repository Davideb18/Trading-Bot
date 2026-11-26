import copy
from datetime import UTC, date, datetime, timedelta
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from freqtrade.prop_profile import build_program_profiles, floors
from freqtrade.prop_risk_approval import policy_identity
from freqtrade.prop_risk_budget import CERTIFIED_BUDGET_MODEL, _evaluate_validated_budget
from scripts import run_natural_benchmark_auto as nat


def _mock_natural_row(cid, slots=3, agg=1200.0, slot_cap=550.0, profit=1500.0, dd=12.0):
    res = {
        "candidate": {
            "id": cid,
            "max_open_trades": slots,
            "stake_amount": "unlimited",
            "max_slot_notional_usdt": slot_cap,
            "max_aggregate_notional_usdt": agg,
            "guard": "standard",
            "macro": "off",
            "dynamic_shield": False,
            "headroom_mode": "standard",
        }
    }
    for window in ("train_1y_funded", "oos_1y_funded", "full_2y_continuous_funded"):
        for suffix in ("", "_two_step"):
            res[window + suffix] = {
                "net_pnl": profit,
                "max_daily_dd_wick": dd,
                "max_overall_dd_wick": dd,
                "funded_margin_gt_25pct_bars": 0,
                "funded_notional_gt_2x_bars": 0,
                "official_daily_breach_wick_bars": 0,
                "official_overall_breach_wick_bars": 0,
                "official_breach_close": None,
                "official_breach_wick": None,
                "funded_withdrawal_policies": {
                    "reset_5250_to_5000": {
                        "withdrawn_net_trader_80pct_usdt": round(profit * 0.8, 2),
                        "payout_count": 5,
                    }
                },
            }
    for group, total in (
        ("program_comparison", 102),
        ("oct_2025_daily_crash_sweep", 31),
        ("autumn_2026_daily_crash_sweep", 57),
    ):
        res[group] = {
            p + "_summary": {
                "total_cohorts": total,
                "resolved_cohorts": max(1, total - 10),
                "qualified_count": max(1, total - 12),
                "qualify_rate_pct": 85.0,
                "resolved_pass_rate_pct": 97.8,
                "first_payout_rate_pct": 80.0,
                "median_days_to_qualify": 42.0,
                "median_days_to_first_payout": 61.0,
                "fail_official": 0,
                "worst_daily_dd_wick": dd,
                "worst_overall_dd_wick": dd,
                "total_official_daily_breach_wick_bars": 0,
                "total_official_overall_breach_wick_bars": 0,
            }
            for p in ("one_step", "two_step")
        }
    return res


def test_every_natural_candidate_passes_live_bot_start_and_prop_risk_budget_bounds():
    """Guarantee 100% live deployment compatibility on AWS without modifying strategy or risk budget."""
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "prop_strategy_nat_check", root / "user_data/strategies/NostalgiaForInfinityX8_Prop.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    strat_cls = module.NostalgiaForInfinityX8_Prop

    base_cfg = json.loads((root / "user_data/config_prop_nfix8.json").read_text(encoding="utf-8"))
    cands = nat.natural_candidates()
    assert len(cands) == 8

    profs = build_program_profiles(
        initial_balance=5000.0,
        start_utc="2026-01-01T00:00:00+00:00",
        one_step_daily_guard=0.036,
        one_step_max_guard=0.065,
    )
    eval_profile = profs["one_step_eval"]
    lim = floors(eval_profile, 5000.0)
    headroom = 5000.0 - max(lim["operating_daily_floor"], lim["operating_overall_floor"])

    for cand in cands:
        assert cand["stake_amount"] == "unlimited"
        ratio = round(float(cand["max_aggregate_notional_usdt"]) / 5000.0, 4)

        # 1. Verify live NostalgiaForInfinityX8_Prop.bot_start() accepts the candidate in live mode
        s = strat_cls.__new__(strat_cls)
        s.config = copy.deepcopy(base_cfg)
        s.config.update(
            max_open_trades=cand["max_open_trades"],
            stake_amount="unlimited",
            tradable_balance_ratio=ratio,
            amend_last_stake_amount=True,
            prop_guard_enabled=True,
            runmode=SimpleNamespace(value="live"),
        )
        s.order_types = dict(base_cfg["order_types"])
        s.bot_start()

        # 2. Verify live freqtrade/prop_risk_budget.py authorizes an initial native slice
        policy = {
            "model": CERTIFIED_BUDGET_MODEL,
            "max_slot_notional_usdt": float(cand["max_slot_notional_usdt"]),
            "max_aggregate_notional_usdt": float(cand["max_aggregate_notional_usdt"]),
            "roundtrip_fee_bps": 12.0,
            "funding_bound_bps": 10.0,
            "execution_slippage_bps": 28.0,
            "latency_gap_stress_bps": 250.0,
            "min_headroom_buffer_usdt": 30.0,
            "status": "approved",
        }
        proposed_stake = float(cand["max_aggregate_notional_usdt"]) / int(cand["max_open_trades"])
        req = {
            "pair": "BTC/USDT:USDT",
            "amount": proposed_stake / 100_000.0,
            "rate": 100_000.0,
            "leverage": 1.0,
            "ordertype": "limit",
            "time_in_force": "GTC",
        }
        _, fp = policy_identity(eval_profile, policy, s.config, req)
        policy["approval"] = {
            "schema_version": 1,
            "policy_fingerprint": fp,
            "kind": "risk_budget_approval",
            "reviewed": True,
            "approved_by": "audit",
            "reference": "natural-grid-test",
            "reviewed_at": datetime.now(UTC).isoformat(),
            "account_id": eval_profile["account_id"],
            "challenge_id": eval_profile["challenge_id"],
        }
        detail = {"headroom": headroom}
        snap = {"positions": [], "open_orders": []}
        _evaluate_validated_budget(eval_profile, policy, req, detail, snap, s.config)
        assert detail.get("authorized") is True, f"{cand['id']} rejected: {detail}"


def test_dual_crash_sweep_validation_and_safety():
    good = _mock_natural_row("NAT_S3_AGG1200_SLOT650_OFF")
    assert nat.complete_row(good)
    assert nat.safe(good)

    # Missing or incomplete Oct 2025 daily crash sweep (must be 31 days) is rejected
    bad_oct_len = copy.deepcopy(good)
    bad_oct_len["oct_2025_daily_crash_sweep"]["one_step_summary"]["total_cohorts"] = 30
    assert not nat.complete_row(bad_oct_len)
    assert not nat.safe(bad_oct_len)

    # Official breach during Oct 2025 crash disqualifies candidate even if 2Y rolling had 0 fails
    bad_oct_breach = copy.deepcopy(good)
    bad_oct_breach["oct_2025_daily_crash_sweep"]["one_step_summary"]["fail_official"] = 1
    assert nat.complete_row(bad_oct_breach)
    assert not nat.safe(bad_oct_breach)


def test_indicator_cache_superset_detection_and_parallel_warmup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(nat, "ROOT", tmp_path)
    monkeypatch.setattr(nat, "OUT", Path("evidence"))
    monkeypatch.setattr(nat, "ENGINE", tmp_path / "engine.py")
    monkeypatch.setattr(nat, "STRATEGY", Path("strat.py"))
    monkeypatch.setattr(nat, "WORKER_STAGGER_SEC", 0.0)
    (tmp_path / "engine.py").write_text("# engine\n", encoding="utf-8")
    strat_path = tmp_path / "strat.py"
    strat_path.write_text("# strat\n", encoding="utf-8")
    mtime = int(strat_path.stat().st_mtime)

    sched = nat.hourly_schedule_path("bluechip")
    nat.atomic_json(
        sched,
        {
            "resolution": "1h",
            "ever_whitelisted_2y": ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"],
            "schedule": {},
        },
    )

    # Cache covering only 2 of 3 pairs -> False (must trigger single-worker warmup first!)
    cdir = tmp_path / ".backtest_cache" / f"2024-11-15_2026-11-15_2_deadbeef_{mtime}"
    cdir.mkdir(parents=True)
    (cdir / "_price_prec.pkl").write_bytes(b"prec")
    nat.atomic_json(
        cdir / "_manifest.json",
        {"complete": True, "universe": ["BTC/USDT:USDT", "ETH/USDT:USDT"]},
    )
    assert not nat._has_indicator_cache_for_schedule(sched)

    call_order = []

    def fake_worker(command, log_path, active_procs, procs_lock):
        cid = command[command.index("--candidates") + 1]
        wjson = Path(command[command.index("--grid-json") + 1])
        call_order.append(cid)
        # First warmup worker populates full superset cache
        if len(call_order) == 1:
            nat.atomic_json(
                cdir / "_manifest.json",
                {"complete": True, "universe": ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]},
            )
        nat.atomic_json(
            wjson,
            {
                "schema_version": 4,
                "train_window": ["2024-11-15 00:00:00+00:00", "2025-11-15 00:00:00+00:00"],
                "oos_window": ["2025-11-15 00:00:00+00:00", "2026-11-15 00:00:00+00:00"],
                "slippage_bps_roundtrip": 10,
                "strict_wick_guard": True,
                "cohort_step_days": 7,
                "schedule_meta": {"path": str(sched)},
                "candidates": [_mock_natural_row(cid, profit=1000.0 + len(call_order))],
            },
        )

    monkeypatch.setattr(nat, "_run_logged_worker", fake_worker)
    ids = ["C1", "C2", "C3"]
    rows = nat.run_natural_grid("bluechip", ids, step=7, workers=3)
    assert call_order[0] == "C1"
    assert [r["candidate"]["id"] for r in rows] == ids
    assert nat._has_indicator_cache_for_schedule(sched)

    # Verify summary & report generation
    entries = [{"preset": "bluechip", "row": r} for r in rows]
    winners = {
        "one_step": {"preset": "bluechip", "candidate": "C3", "score": nat.score(rows[2], "one_step")},
        "two_step": {"preset": "bluechip", "candidate": "C3", "score": nat.score(rows[2], "two_step")},
    }
    summary, report = nat.build_summary_and_report(entries, winners, {})
    assert len(summary["leaderboard"]) == 3
    assert "REPORT NFIX8-NATURAL" in report
    assert "NON CERTIFICATO LIVE" in report
    assert (nat.OUT / "natural_benchmark_summary.json").is_file()
    assert (nat.OUT / "natural_benchmark_report.txt").is_file()


def test_presets_start_with_base_and_share_revision_safe_indicator_cache(tmp_path, monkeypatch):
    assert nat.PRESETS_ORDER == ("base", "strict", "bluechip")
    monkeypatch.setattr(nat, "ROOT", tmp_path)
    engine = tmp_path / "engine.py"
    engine.write_text("# engine\n", encoding="utf-8")
    monkeypatch.setattr(nat, "ENGINE", engine)
    strat = tmp_path / nat.STRATEGY
    strat.parent.mkdir(parents=True)
    strat.write_text("# strat\n", encoding="utf-8")
    cfg = tmp_path / "user_data/config_prop_nfix8.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("{}", encoding="utf-8")
    data = tmp_path / "user_data/data/bybit/futures/BTC_USDT_USDT-5m-futures.feather"
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_bytes(b"v1")

    d1 = nat.indicator_cache_dir()
    # Changing research/mcap flags does not change raw indicator preprocessing cache
    monkeypatch.setattr(nat, "RESEARCH_MODE", True)
    monkeypatch.setattr(nat, "MCAP_MODE", "hybrid")
    assert nat.indicator_cache_dir() == d1

    # Changing data or strategy strictly rotates indicator cache directory
    data.write_bytes(b"v2-changed")
    d2 = nat.indicator_cache_dir()
    assert d2 != d1
    strat.write_text("# strat v2\n", encoding="utf-8")
    assert nat.indicator_cache_dir() != d2


def test_summary_line_includes_cohort_dd_inactivity_and_open_end():
    row = _mock_natural_row("NAT_S3_AGG1200_SLOT650_OFF", profit=1250.0, dd=15.0)
    row["program_comparison"]["one_step_summary"].update(
        worst_overall_dd_wick=142.5,
        fail_inactivity_30d=4,
        open_end=9,
    )
    line = nat._summarize_completed_row(row)
    assert "DD(2y/Coorti) 15.00$/142.50$" in line
    assert "inatt: 4, open: 9" in line


def test_worker_interrupt_exit_code_aborts_without_retry(tmp_path, monkeypatch):
    class FakeProc:
        def wait(self):
            return 3221225786  # Windows STATUS_CONTROL_C_EXIT

    monkeypatch.setattr(nat.subprocess, "Popen", lambda *a, **kw: FakeProc())
    with pytest.raises(KeyboardInterrupt):
        nat._run_logged_worker(["python", "-V"], tmp_path / "w.log", set(), nat.threading.Lock())


def test_inplace_subset_indicator_cache_hit_and_no_recompute(tmp_path, monkeypatch):
    import pickle
    import numpy as np
    import pandas as pd
    from scripts import prop_backtest_1to1 as engine

    cache_dir = tmp_path / "natural-ind-shared"
    cache_dir.mkdir(parents=True)
    ts_base = np.array([1731628800, 1731629100], dtype=np.int64)
    ohlc = np.array([[100.0, 101.0, 99.0, 100.5], [100.5, 102.0, 100.0, 101.5]], dtype=np.float64)
    sigs = np.zeros((2, 4), dtype=np.int8)
    etags = np.zeros(2, dtype=np.int32)
    xtags = np.zeros(2, dtype=np.int32)
    tags = [""]
    files = {}
    for pair in ("BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"):
        ps = pair.replace("/", "_").replace(":", "_")
        arr = engine.CompactOhlcvArray(ts_base, ohlc, sigs, etags, xtags, tags)
        ohl_name = f"ohlcv_{ps}.npz"
        an_name = f"an_{ps}.feather"
        arr.save_npz(cache_dir / ohl_name)
        pd.DataFrame({"close": [100.5, 101.5]}).to_feather(cache_dir / an_name)
        files[pair] = {"ohlcv": ohl_name, "analyzed": an_name}
    (cache_dir / "_price_prec.pkl").write_bytes(
        pickle.dumps({"BTC/USDT:USDT": 0.1, "ETH/USDT:USDT": 0.01, "SOL/USDT:USDT": 0.01})
    )
    nat.atomic_json(
        cache_dir / "_manifest.json",
        {
            "complete": True,
            "universe": ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"],
            "available_pairs": ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"],
            "min_date": "2024-11-15T00:00:00+00:00",
            "max_date": "2024-11-15T00:05:00+00:00",
            "files": files,
        },
    )

    monkeypatch.setattr(engine, "_install_fast_custom_data_index", lambda: None)
    monkeypatch.setattr(
        engine.history,
        "load_pair_history",
        lambda **kw: pd.DataFrame(),
    )
    strat = SimpleNamespace(
        advise_all_indicators=lambda *a, **kw: pytest.fail("Should not recompute indicators on subset hit")
    )
    dp = SimpleNamespace(get_analyzed_dataframe=lambda pair, tf: (pd.DataFrame(), None))
    bt = SimpleNamespace(
        config={"candle_type_def": "futures"},
        strategylist=[strat],
        timeframe="5m",
        timerange=SimpleNamespace(adjust_start_if_necessary=lambda *a: None),
        required_startup=0,
        dataprovider=dp,
        _set_strategy=lambda s: None,
        _load_bt_data_detail=lambda: None,
    )
    cfg = {"datadir": tmp_path, "dataformat_ohlcv": "feather"}
    subset = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
    ohlcv_out, _, min_dt, max_dt = engine._prepare_universe_cached(bt, cfg, subset, cache_dir)
    assert set(ohlcv_out.keys()) == set(subset)
    assert bt.available_pairs == subset
    assert set(bt.price_pair_prec.keys()) == set(subset)
    assert int(min_dt.timestamp()) == int(ts_base[0])
    assert int(max_dt.timestamp()) == int(ts_base[-1])
    # Manifest stays intact with all 3 superset pairs (zero writes on subset hit!)
    assert len(nat.read_json(cache_dir / "_manifest.json")["universe"]) == 3
