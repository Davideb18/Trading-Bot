"""Regression checks for historical evidence and resumable natural benchmarks.

Workers and market downloads are mocked; these tests never run a benchmark.
"""

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from scripts import prop_prepare_benchmark_data as prep
from scripts import run_natural_benchmark_auto as nat
from scripts.tests.test_natural_benchmark_auto import _mock_natural_row


@pytest.mark.parametrize("resolution", ["1d", "1h"])
@pytest.mark.parametrize("rejection", ["market_cap", "instrument", "age"])
def test_top_volume_rejected_pair_is_not_backfilled(tmp_path, monkeypatch, resolution, rejection):
    monkeypatch.setattr(prep, "FUTURES_DIR", tmp_path)
    valid = dict(contractType="LinearPerpetual", quoteCoin="USDT", settleCoin="USDT", symbolType="", tags=[])
    instruments = {"AAA": valid, "BBB": valid}
    if rejection == "instrument":
        instruments["AAA"] = dict(valid, symbolType="tradfi")
    monkeypatch.setattr(prep, "fetch_or_load_bybit_instruments", lambda: instruments)
    caps = {s: pd.Series([500_000_000.0], index=pd.to_datetime(["2025-01-13"], utc=True)) for s in instruments}
    if rejection == "market_cap":
        caps["AAA"].iloc[0] = 1.0
    monkeypatch.setattr(prep, "load_coinmetrics_marketcap_map", lambda _: caps)
    config = tmp_path / "config.json"
    config.write_text('{"exchange": {"pair_blacklist": []}}')
    for symbol, volume in (("AAA", 2_000_000), ("BBB", 1_000_000)):
        days = 9 if symbol == "AAA" and rejection == "age" else 12
        daily = pd.DataFrame({"date": pd.date_range(end="2025-01-12", periods=days, tz="UTC"),
                              "open": 100., "high": 101., "low": 99., "close": 100., "volume": volume})
        daily.to_feather(tmp_path / f"{symbol}_USDT_USDT-1d-futures.feather")
        daily.to_feather(tmp_path / f"{symbol}_USDT_USDT-5m-futures.feather")
        hourly = pd.DataFrame({"date": pd.date_range("2025-01-12", periods=48, freq="h", tz="UTC"),
                               "close": 100., "volume": volume})
        hourly.to_feather(tmp_path / f"{symbol}_USDT_USDT-1h-futures.feather")
    result = prep.build_point_in_time_schedule(config_path=config, start_iso="2025-01-13",
        end_iso="2025-01-13", min_days_listed=10, number_assets=1, resolution=resolution)
    assert all(pairs == [] for pairs in result["schedule"].values())


def worker_doc(schedule, profit=1500.):
    return {"schema_version": 4, "cohort_step_days": 7, "slippage_bps_roundtrip": 10,
            "strict_wick_guard": True, "train_window": ["2024-11-15"], "oos_window": ["2026-11-15"],
            "schedule_meta": {"path": str(schedule)}, "candidates": [_mock_natural_row("C1", profit=profit)]}


@pytest.mark.parametrize("signature", [None, "old", "current"])
@pytest.mark.parametrize("source", ["worker", "merged"])
def test_resume_requires_matching_signature(tmp_path, monkeypatch, signature, source):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(nat, "OUT", Path("evidence"))
    monkeypatch.setattr(nat, "engine_signature", lambda *a, **kw: "current")
    monkeypatch.setattr(nat, "common_args", lambda *a: [])
    schedule = nat.hourly_schedule_path("base")
    worker = nat.OUT / ("_workers/grid_natural_base_1h.C1.json" if source == "worker" else "grid_natural_base_1h.json")
    nat.atomic_json(worker, worker_doc(schedule, profit=1.))
    if signature:
        nat.atomic_json(worker.with_suffix(".json.run.json"), {"engine_signature": signature})
    calls = []

    def run(command, *args):
        calls.append(command)
        nat.atomic_json(Path(command[command.index("--grid-json") + 1]), worker_doc(schedule, profit=2.))

    monkeypatch.setattr(nat, "_run_logged_worker", run)
    result = nat.run_natural_grid("base", ["C1"], step=7)
    assert len(calls) == (0 if signature == "current" else 1)
    assert result[0]["full_2y_continuous_funded"]["net_pnl"] == (1. if signature == "current" else 2.)


def test_signature_changes_with_strategy_config_and_data(tmp_path, monkeypatch):
    monkeypatch.setattr(nat, "ROOT", tmp_path)
    engine = tmp_path / "engine.py"
    engine.write_text("# engine")
    monkeypatch.setattr(nat, "ENGINE", engine)
    strategy = tmp_path / nat.STRATEGY
    strategy.parent.mkdir(parents=True)
    strategy.write_text("# strategy")
    schedule = tmp_path / "schedule.json"
    schedule.write_text("{}")
    config = tmp_path / "user_data/config_prop_nfix8.json"
    config.write_text("{}")
    signature = lambda: nat.engine_signature(schedule, kind="test", step=7)
    old = signature()
    for path in (strategy, config):
        path.write_text(path.read_text() + " ")
        new = signature()
        assert new != old
        old = new
    data = tmp_path / "user_data/data/bybit/futures/BTC-1d.feather"
    data.parent.mkdir(parents=True)
    data.write_bytes(b"new data")
    assert signature() != old


def test_natural_runner_rejects_proxy_schedules_and_missing_archived_funding(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(nat, "read_json", lambda path: {"resolution": "1h", "zero_lookahead": False, "mcap_mode": "hybrid"})
    monkeypatch.setattr(nat, "_archive_issues_and_pairs", lambda: ([], set()))
    assert sum("historical evidence" in issue for issue in nat.hourly_schedule_issues()) == 3
    assert "--allow-research-schedule" not in nat.common_args()
    issues = nat._futures_files_issues({"OM/USDT:USDT"}, {"OM/USDT:USDT"})
    assert any("mark/funding_rate" in issue for issue in issues)


def test_preparation_requests_warmup_and_strict_caps(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(nat, "OUT", Path("evidence"))
    monkeypatch.setattr(nat, "hourly_schedule_issues", lambda: ["unverified historical evidence"])
    monkeypatch.setattr(nat, "raw_market_data_present", lambda: False)
    scripts, commands = [], []
    monkeypatch.setattr(nat, "run_script", lambda *args: scripts.append(args))
    monkeypatch.setattr(nat, "run", lambda command: commands.append(command))
    with pytest.raises(RuntimeError, match="nessun backtest avviato"):
        nat.prepare_data_and_hourly_schedules()
    timerange = commands[0][commands[0].index("--timerange") + 1]
    start = date.fromisoformat(timerange.split("-")[0])
    assert (date(2024, 11, 15) - start).days >= 120
    build = scripts[-1]
    assert build[build.index("--mcap-mode") + 1] == "strict"
    assert "--fetch-coingecko" not in build
    assert "--export-volume-universe" in scripts[0]


def test_volume_download_universe_keeps_instruments_for_initial_ranking(tmp_path, monkeypatch):
    valid = dict(contractType="LinearPerpetual", quoteCoin="USDT", settleCoin="USDT", symbolType="", tags=[])
    monkeypatch.setattr(prep, "fetch_or_load_bybit_instruments", lambda: {
        "BTC": valid, "AAPL": dict(valid, symbolType="tradfi"),
        "NEW": dict(valid, isPreListing=True), "WRONG": dict(valid, quoteCoin="USDC"),
    })
    config = tmp_path / "config.json"
    config.write_text('{"exchange": {"pair_blacklist": []}}')
    pairs = prep.export_eligible_download_pairs(config, tmp_path / "pairs.json", volume_universe=True)
    assert pairs == ["AAPL/USDT:USDT", "BTC/USDT:USDT"]


@pytest.mark.parametrize("days, expected", [(14, False), (120, True)])
def test_existing_data_needs_daily_warmup(tmp_path, monkeypatch, days, expected):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(nat, "_archive_issues_and_pairs", lambda: ([], set()))
    monkeypatch.setattr(nat, "_futures_files_issues", lambda *args: [])
    futures = Path("user_data/data/bybit/futures")
    futures.mkdir(parents=True)
    pd.DataFrame({"date": pd.date_range(end="2024-11-14", periods=days, tz="UTC")}).to_feather(
        futures / "BTC_USDT_USDT-1d-futures.feather")
    assert nat.raw_market_data_present() is expected


@pytest.mark.parametrize("missing", ["mark", "funding"])
def test_missing_market_history_fails_without_synthetic_fallback(monkeypatch, missing):
    from freqtrade.enums import TradingMode
    from freqtrade.exceptions import OperationalException
    from scripts import prop_backtest_1to1 as engine

    pair = "OM/USDT:USDT"
    frame = pd.DataFrame({"date": pd.to_datetime(["2025-01-01"], utc=True), "open": [1.]})
    options = {"funding_fee_timeframe": "8h", "mark_ohlcv_timeframe": "1h", "mark_ohlcv_price": "mark",
               "uses_leverage_tiers": False}
    fake = SimpleNamespace(config={"datadir": "unused", "dataformat_ohlcv": "feather"},
        trading_mode=TradingMode.FUTURES, timerange=None, pairlists=SimpleNamespace(whitelist=[pair]),
        exchange=SimpleNamespace(get_option=lambda key, default=None: options.get(key, default)),
        prop_synthetic_mark_pairs=[], prop_synthetic_funding_pairs=[])
    responses = iter([{}, {pair: frame}] if missing == "funding" else [{pair: frame}, {}])
    monkeypatch.setattr(engine.history, "load_data", lambda **kw: next(responses))
    monkeypatch.setattr(engine.history, "load_pair_history", lambda **kw: pytest.fail("Synthetic fallback reached"))
    with pytest.raises(OperationalException, match="synthetic data disabled"):
        engine.PropParityBacktesting._load_bt_data_detail(fake)


@pytest.mark.parametrize("value", [None, False, "true"])
def test_missing_or_invalid_schedule_evidence_fails_closed(tmp_path, value):
    from freqtrade.exceptions import OperationalException
    from scripts.prop_backtest_1to1 import run_suite

    path = tmp_path / "schedule.json"
    nat.atomic_json(path, {"zero_lookahead": value, "schedule": {}})
    with pytest.raises(OperationalException, match="Historical schedule is unverified"):
        run_suite(SimpleNamespace(config="unused", pairlist_schedule=str(path)))
