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
