import sys
from pathlib import Path
import numpy as np
import pandas as pd
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prop_callback_snapshot import CallbackSnapshot


def test_mixed_rows_preserve_values_dtypes_index_and_nan():
    df = pd.DataFrame({'date': pd.date_range('2025-01-01', periods=12, tz='UTC'),
                       'price': np.arange(12, dtype=float), 'tag': ['x', None] * 6,
                       'flag': [True, False] * 6, 'missing': np.nan})
    df.index = pd.Index(range(100, 112), name='candle')
    cached = CallbackSnapshot(df)
    for index in range(-12, 12):
        pd.testing.assert_series_equal(cached.iloc[index], df.iloc[index])
    pd.testing.assert_frame_equal(cached.iloc[-3:], df.iloc[-3:])
    pd.testing.assert_series_equal(cached.iloc[:, 1], df.iloc[:, 1])
    with pytest.raises(IndexError):
        _ = cached.iloc[12]
    assert len(cached._callback_rows) <= 8


def test_callback_series_mutation_does_not_pollute_cache_or_source():
    df = pd.DataFrame({'price': [1., 2.], 'tag': ['a', 'b']})
    cached = CallbackSnapshot(df)
    first = cached.iloc[-1]
    first['price'] = -999
    pd.testing.assert_series_equal(cached.iloc[-1], df.iloc[-1])
    assert df.iloc[-1]['price'] == 2.


def test_new_snapshot_does_not_leak_future_or_previous_cohort_rows():
    df = pd.DataFrame({'price': [1., 2., 3.]})
    late = CallbackSnapshot(df.iloc[:3])
    early = CallbackSnapshot(df.iloc[:1])
    assert late.iloc[-1]['price'] == 3.
    assert early.iloc[-1]['price'] == 1.
