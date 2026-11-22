"""Bounded row cache for read-only analyzed snapshots in offline benchmarks."""
from collections import OrderedDict
import pandas as pd


class CallbackSnapshot(pd.DataFrame):
    """Cache scalar row extraction; return independent Series to each callback.

    Only wrap immutable, already-sliced indicator data. Never use for live data,
    indicator construction, or callbacks that mutate their input dataframe.
    """

    _metadata = ['_callback_rows']

    @property
    def _constructor(self):
        return pd.DataFrame

    def __init__(self, frame):
        super().__init__(frame, copy=False)
        self._callback_rows = OrderedDict()

    def _ixs(self, i, axis=0):
        if axis != 0:
            return super()._ixs(i, axis=axis)
        position = i if i >= 0 else len(self) + i
        if position < 0 or position >= len(self):
            return super()._ixs(i, axis=axis)
        if position not in self._callback_rows:
            self._callback_rows[position] = super()._ixs(i, axis=axis).copy(deep=True)
            while len(self._callback_rows) > 8:
                self._callback_rows.popitem(last=False)
        else:
            self._callback_rows.move_to_end(position)
        return self._callback_rows[position].copy(deep=True)
