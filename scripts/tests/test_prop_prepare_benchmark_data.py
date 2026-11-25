import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from scripts import prop_prepare_benchmark_data as prep


@pytest.fixture
def schedule_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(prep, "FUTURES_DIR", tmp_path)
    monkeypatch.setattr(prep, "fetch_or_load_bybit_instruments", lambda: {"OM": {
        "contractType": "LinearPerpetual", "quoteCoin": "USDT", "settleCoin": "USDT",
        "symbolType": "", "tags": []}})
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"exchange": {"pair_blacklist": ["OM/.*"]}}))
    prices = np.linspace(100, 110, 12)
    frame = pd.DataFrame({"date": pd.date_range("2025-01-01", periods=12, tz="UTC"),
                          "open": prices, "high": prices + 1, "low": prices - 1,
                          "close": prices, "volume": 1_000_000})
    frame.to_feather(tmp_path / "OM_USDT_USDT-1d-futures.feather")
    frame.to_feather(tmp_path / "OM_USDT_USDT-5m-futures.feather")
    return config


@pytest.mark.parametrize("caps", [{}, {"OM": pd.Series([float("nan")], index=pd.to_datetime(["2025-01-13"], utc=True))},
                                  {"OM": pd.Series([200_000_000], index=pd.to_datetime(["2025-01-10"], utc=True))}])
def test_unknown_invalid_or_stale_cap_never_admitted(schedule_inputs, monkeypatch, caps):
    monkeypatch.setattr(prep, "load_coinmetrics_marketcap_map", lambda _: caps)
    result = prep.build_point_in_time_schedule(config_path=schedule_inputs, start_iso="2025-01-13",
        end_iso="2025-01-13", min_days_listed=10, anti_survivorship=True)
    assert list(result["schedule"].values()) == [[]]


def test_historical_altcoin_not_removed_by_current_blacklist(schedule_inputs, monkeypatch):
    caps = {"OM": pd.Series([200_000_000], index=pd.to_datetime(["2025-01-13"], utc=True))}
    monkeypatch.setattr(prep, "load_coinmetrics_marketcap_map", lambda _: caps)
    result = prep.build_point_in_time_schedule(config_path=schedule_inputs, start_iso="2025-01-13",
        end_iso="2025-01-13", min_days_listed=10, anti_survivorship=True)
    assert list(result["schedule"].values()) == [["OM/USDT:USDT"]]
    assert result["validation_status"] != "validated"


def test_archive_conversion_never_fabricates_mark_or_funding(tmp_path, monkeypatch):
    root = tmp_path / "archive" / "OMUSDT"
    root.mkdir(parents=True)
    output = tmp_path / "futures"
    monkeypatch.setattr(prep, "FORENSIC_DIR", tmp_path)
    monkeypatch.setattr(prep, "FUTURES_DIR", output)
    frame = pd.DataFrame({"date": pd.date_range("2025-01-01", periods=2, freq="min", tz="UTC"),
                          "open": [1., 2.], "high": [1., 2.], "low": [1., 2.],
                          "close": [1., 2.], "volume": [10., 20.]})
    frame.to_feather(root / "day-1m.feather")
    assert prep.convert_archived_1m_to_freqtrade_feathers() == ["OM/USDT:USDT"]
    assert not list(output.glob("*-mark.feather"))
    assert not list(output.glob("*-funding_rate.feather"))
    assert len(pd.read_feather(output / "OM_USDT_USDT-1m-futures.feather")) == 2


@pytest.mark.parametrize("info", [None, {}, {"contractType": "LinearPerpetual",
    "quoteCoin": "USDT", "settleCoin": "USDT"}])
def test_unknown_instrument_classification_is_not_eligible(info):
    assert not prep.is_eligible_bybit_crypto(info)


def test_unverified_schedule_cannot_produce_benchmark_results(tmp_path):
    from freqtrade.exceptions import OperationalException
    from scripts.prop_backtest_1to1 import run_suite

    schedule = tmp_path / "schedule.json"
    schedule.write_text(json.dumps({"zero_lookahead": False, "schedule": {}}))
    with pytest.raises(OperationalException, match="Historical schedule is unverified"):
        run_suite(SimpleNamespace(config="unused.json", pairlist_schedule=str(schedule)))


def test_archived_delisted_coin_admitted_only_when_flag_enabled(tmp_path, monkeypatch):
    fut = tmp_path / "futures"
    fut.mkdir()
    arch = tmp_path / "archive" / "OMUSDT"
    arch.mkdir(parents=True)
    monkeypatch.setattr(prep, "FORENSIC_DIR", tmp_path)
    monkeypatch.setattr(prep, "FUTURES_DIR", fut)
    monkeypatch.setattr(prep, "fetch_or_load_bybit_instruments", lambda: {})
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"exchange": {"pair_blacklist": ["OM/.*"]}}))
    prices = np.linspace(100, 110, 12)
    frame = pd.DataFrame({
        "date": pd.date_range("2025-01-01", periods=12, tz="UTC"),
        "open": prices, "high": prices + 1, "low": prices - 1,
        "close": prices, "volume": 1_000_000,
    })
    frame.to_feather(fut / "OM_USDT_USDT-1d-futures.feather")
    frame.to_feather(fut / "OM_USDT_USDT-5m-futures.feather")
    frame.to_feather(arch / "OMUSDT2025-01-01-1m.feather")
    caps = {"OM": pd.Series([200_000_000], index=pd.to_datetime(["2025-01-13"], utc=True))}
    monkeypatch.setattr(prep, "load_coinmetrics_marketcap_map", lambda _: caps)

    without_flag = prep.build_point_in_time_schedule(
        config_path=config, start_iso="2025-01-13", end_iso="2025-01-13",
        min_days_listed=10, anti_survivorship=True, include_archived_delisted=False,
    )
    assert list(without_flag["schedule"].values()) == [[]]

    with_flag = prep.build_point_in_time_schedule(
        config_path=config, start_iso="2025-01-13", end_iso="2025-01-13",
        min_days_listed=10, anti_survivorship=True, include_archived_delisted=True,
    )
    assert list(with_flag["schedule"].values()) == [["OM/USDT:USDT"]]
    assert with_flag["archived_delisted_included"] == ["OM/USDT:USDT"]


def test_hybrid_mcap_handles_post_cutoff_and_keeps_fresh_coinmetrics_authoritative(tmp_path, monkeypatch):
    fut = tmp_path / "futures"
    fut.mkdir()
    monkeypatch.setattr(prep, "FORENSIC_DIR", tmp_path)
    monkeypatch.setattr(prep, "FUTURES_DIR", fut)
    valid_inst = {"contractType": "LinearPerpetual", "quoteCoin": "USDT", "settleCoin": "USDT",
                  "symbolType": "", "tags": []}
    monkeypatch.setattr(prep, "fetch_or_load_bybit_instruments",
                        lambda: {"BTC": valid_inst, "LOW": valid_inst, "NEWC": valid_inst})
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"exchange": {"pair_blacklist": []}}))
    dates = pd.date_range("2026-05-01", periods=40, tz="UTC")
    prices = np.linspace(100, 105, 40)
    frame = pd.DataFrame({
        "date": dates, "open": prices, "high": prices + 1, "low": prices - 1,
        "close": prices, "volume": 1_000_000,
    })
    for sym in ("BTC", "LOW", "NEWC"):
        frame.to_feather(fut / f"{sym}_USDT_USDT-1d-futures.feather")
        frame.to_feather(fut / f"{sym}_USDT_USDT-5m-futures.feather")

    # BTC CoinMetrics ends 2026-05-20 (before 2026-06-05); LOW has fresh <$100M on 2026-06-05;
    # NEWC is absent from CoinMetrics but >=$100M in CoinGecko snapshot.
    caps = {
        "BTC": pd.Series([1_500_000_000_000.0], index=pd.to_datetime(["2026-05-20"], utc=True)),
        "LOW": pd.Series([50_000_000.0], index=pd.to_datetime(["2026-06-05"], utc=True)),
    }
    monkeypatch.setattr(prep, "load_coinmetrics_marketcap_map", lambda _: caps)
    res = prep.build_point_in_time_schedule(
        config_path=config, start_iso="2026-06-05", end_iso="2026-06-05",
        min_days_listed=10, anti_survivorship=True, mcap_mode="hybrid",
        coingecko_mcap_map={"BTC": 1_600_000_000_000.0, "LOW": 250_000_000.0, "NEWC": 300_000_000.0},
    )
    admitted = list(res["schedule"].values())[0]
    assert "BTC/USDT:USDT" in admitted
    assert "NEWC/USDT:USDT" in admitted
    # Fresh CoinMetrics (<$100M) MUST NOT be overridden by CoinGecko proxy
    assert "LOW/USDT:USDT" not in admitted
    assert res["coverage_diagnostics"]["empty_days_post_2026_05"] == 0


def test_export_eligible_download_pairs_filters_tradfi_and_innovation(tmp_path, monkeypatch):
    monkeypatch.setattr(prep, "fetch_or_load_bybit_instruments", lambda: {
        "BTC": {"contractType": "LinearPerpetual", "quoteCoin": "USDT", "settleCoin": "USDT", "symbolType": "", "tags": []},
        "ETH": {"contractType": "LinearPerpetual", "quoteCoin": "USDT", "settleCoin": "USDT", "symbolType": "", "tags": []},
        "AAPL": {"contractType": "LinearPerpetual", "quoteCoin": "USDT", "settleCoin": "USDT", "symbolType": "tradfi", "tags": []},
        "TINY": {"contractType": "LinearPerpetual", "quoteCoin": "USDT", "settleCoin": "USDT", "symbolType": "innovation", "tags": ["Innovation"]},
        "USDC": {"contractType": "LinearPerpetual", "quoteCoin": "USDT", "settleCoin": "USDT", "symbolType": "", "tags": []},
    })
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"exchange": {"pair_blacklist": []}}))
    out = tmp_path / "pairs.json"
    pairs = prep.export_eligible_download_pairs(cfg, out, anti_survivorship=True)
    assert pairs == ["BTC/USDT:USDT", "ETH/USDT:USDT"]
    assert json.loads(out.read_text()) == ["BTC/USDT:USDT", "ETH/USDT:USDT"]


def test_hourly_schedule_enforces_zero_lookahead_rolling_volume_and_delist_cutoff(tmp_path, monkeypatch):
    from datetime import UTC, datetime
    from scripts.prop_backtest_1to1 import HistoricalPairListAdapter

    fut = tmp_path / "futures"
    fut.mkdir()
    monkeypatch.setattr(prep, "FORENSIC_DIR", tmp_path)
    monkeypatch.setattr(prep, "FUTURES_DIR", fut)
    valid_inst = {
        "contractType": "LinearPerpetual",
        "quoteCoin": "USDT",
        "settleCoin": "USDT",
        "symbolType": "",
        "tags": [],
    }
    monkeypatch.setattr(
        prep,
        "fetch_or_load_bybit_instruments",
        lambda: {"AAA": valid_inst, "BBB": valid_inst},
    )
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"exchange": {"pair_blacklist": []}}), encoding="utf-8")

    # 12 daily candles 2025-01-01..2025-01-12 -> usable on 2025-01-13
    days = pd.date_range("2025-01-01", periods=12, freq="1D", tz="UTC")
    prices = np.linspace(100.0, 105.0, 12)
    df_1d = pd.DataFrame(
        {
            "date": days,
            "open": prices,
            "high": prices + 1.0,
            "low": prices - 1.0,
            "close": prices,
            "volume": 500_000.0,
        }
    )
    for sym in ("AAA", "BBB"):
        df_1d.to_feather(fut / f"{sym}_USDT_USDT-1d-futures.feather")
        df_1d.to_feather(fut / f"{sym}_USDT_USDT-5m-futures.feather")

    # Hourly bars from 2025-01-12 00:00 through 2025-01-13 23:00:
    # AAA has steady $1.5M/h ($36M/24h >= $20M) all day.
    # BBB starts low ($0.5M/h = $12M/24h < $20M), spikes +$30M on candle opening 2025-01-13 10:00
    # (which closes at 11:00 UTC -> MUST NOT be visible at 10:00 UTC, MUST become #1 at 11:00 UTC!),
    # and stops trading after candle opening 2025-01-13 15:00 (last usable at 16:00 UTC, dropped at 17:00 UTC!).
    hours_aaa = pd.date_range("2025-01-12 00:00", "2025-01-13 23:00", freq="1h", tz="UTC")
    pd.DataFrame(
        {
            "date": hours_aaa,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 15_000.0,  # $1.5M/h -> $36M/24h
        }
    ).to_feather(fut / "AAA_USDT_USDT-1h-futures.feather")

    hours_bbb = pd.date_range("2025-01-12 00:00", "2025-01-13 15:00", freq="1h", tz="UTC")
    vols_bbb = np.full(len(hours_bbb), 5_000.0)  # $0.5M/h -> $12M/24h
    spike_idx = int((pd.Timestamp("2025-01-13 10:00", tz="UTC") - hours_bbb[0]) / pd.Timedelta(hours=1))
    vols_bbb[spike_idx] = 300_000.0  # +$30M during [10:00, 11:00) -> usable at 11:00 UTC!
    pd.DataFrame(
        {
            "date": hours_bbb,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": vols_bbb,
        }
    ).to_feather(fut / "BBB_USDT_USDT-1h-futures.feather")

    res = prep.build_point_in_time_schedule(
        config_path=cfg,
        start_iso="2025-01-13",
        end_iso="2025-01-13",
        min_days_listed=10,
        min_value_usdt=20_000_000.0,
        anti_survivorship=True,
        mcap_mode="none",
        resolution="1h",
    )
    assert res["resolution"] == "1h"
    adapter = HistoricalPairListAdapter(res["ever_whitelisted_2y"], res["schedule"])
    adapter.active_dynamic = True

    def wl_at(hour: int) -> list[str]:
        adapter.current_time = datetime(2025, 1, 13, hour, 0, tzinfo=UTC)
        adapter.refresh_pairlist()
        return adapter.whitelist

    # At 10:00 UTC the [10:00, 11:00) spike candle is NOT closed yet -> zero lookahead!
    assert wl_at(0) == ["AAA/USDT:USDT"]
    assert wl_at(10) == ["AAA/USDT:USDT"]
    # At 11:00 UTC the [10:00, 11:00) candle closed -> BBB jumps to #1 ahead of AAA!
    assert wl_at(11) == ["BBB/USDT:USDT", "AAA/USDT:USDT"]
    assert wl_at(16) == ["BBB/USDT:USDT", "AAA/USDT:USDT"]
    # At 17:00 UTC BBB has no closed 1h data (halted/delisted after 16:00 close) -> immediately removed!
    assert wl_at(17) == ["AAA/USDT:USDT"]

