import pandas as pd
import pytest
from scripts.prop_repair_archive_minutes import reconstruct


def test_reconstruction_preserves_observed_prices_and_volume():
    dates = pd.to_datetime(["2025-01-01 00:00", "2025-01-01 00:03"], utc=True)
    frame = pd.DataFrame({"date": dates, "open": [10., 20.], "high": [11., 22.],
                          "low": [9., 18.], "close": [10.5, 21.], "volume": [5., 8.]})
    result, count = reconstruct(frame, {dates[0].floor("D")})
    assert count == 2
    pd.testing.assert_frame_equal(result.iloc[[0, 3]].reset_index(drop=True), frame)
    assert (result.iloc[1:3][["open", "high", "low", "close"]] == 10.5).all().all()
    assert result.volume.sum() == 13.
    again, count = reconstruct(result, {dates[0].floor("D")})
    assert count == 0
    pd.testing.assert_frame_equal(again, result)
    with pytest.raises(ValueError, match="outside verified"):
        reconstruct(frame, set())
