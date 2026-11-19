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
