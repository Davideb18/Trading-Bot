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


def test_download_delete_raw_removes_csv_gz_but_keeps_feather_and_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "ROOT", tmp_path)
    directory = tmp_path / "archive" / "OMUSDT"
    directory.mkdir(parents=True)
    filename = "OMUSDT2025-04-13.csv.gz"
    raw_path = directory / filename
    pd.DataFrame(dict(timestamp=[1744502400, 1744502410], price=[6.0, 0.5], size=[10, 20],
                      foreignNotional=[60, 10])).to_csv(raw_path, compression="gzip", index=False)
    assert raw_path.exists()
    result = archive.download(("OMUSDT", filename), delete_raw=True)
    assert not raw_path.exists()
    assert result["raw_deleted"] is True
    assert result["bytes"] > 0
    assert len(result["sha256"]) == 64
    bars = pd.read_feather(result["output"])
    assert len(bars) == 1
    assert bars.iloc[0]["high"] == 6.0
    assert bars.iloc[0]["low"] == 0.5
