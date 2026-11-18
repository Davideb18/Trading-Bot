import json

import pandas as pd
import pytest

from scripts import prop_archive_data as archive


def test_trade_archive_reconstructs_wicks_and_turnover_without_filling_gaps(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "ROOT", tmp_path)
    directory = tmp_path / "archive" / "TESTUSDT"
    directory.mkdir(parents=True)
    filename = "TESTUSDT2024-11-01.csv.gz"
    pd.DataFrame(dict(timestamp=[1730419200, 1730419201, 1730419320],
                      price=[100, 50, 80], size=[1, 2, 3],
                      foreignNotional=[100, 100, 240])).to_csv(
                          directory / filename, compression="gzip", index=False)
    result = archive.download(("TESTUSDT", filename))
    bars = pd.read_feather(result["output"])
    assert result["bars"] == 2
    assert result["missing_internal_minutes"] == 1
    assert bars.iloc[0]["high"] == 100
    assert bars.iloc[0]["low"] == 50
    assert bars.turnover.sum() == 440
    assert result["trades"] == 3
    assert len(result["sha256"]) == 64


@pytest.mark.parametrize("symbol", ["../BTCUSDT", "BTC/USD", "BTCUSDT?x=1"])
def test_inventory_rejects_non_symbol_paths(symbol):
    with pytest.raises(ValueError, match="Invalid USDT symbol"):
        archive.inventory(symbol)
