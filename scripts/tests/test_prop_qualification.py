from datetime import UTC, datetime, timedelta, timezone

import pytest

from freqtrade.prop_qualification import qualifying_close_day


def qualify(**overrides):
    values = dict(is_open=False, close_dt=datetime(2026, 11, 18, tzinfo=UTC),
                  remaining_qty=0.0, exit_events=[dict(entry_notional=250, gross_pnl=2.5)],
                  initial_balance=5000)
    values.update(overrides)
    return qualifying_close_day(**values)


@pytest.mark.parametrize("gross", [2.5, -2.5])
def test_gross_threshold_both_directions(gross):
    assert qualify(exit_events=[dict(entry_notional=250, gross_pnl=gross)]) == "2026-11-18"


@pytest.mark.parametrize("overrides", [dict(is_open=True), dict(remaining_qty=0.1),
    dict(close_dt=None), dict(exit_events=[]),
    dict(exit_events=[dict(entry_notional=249, gross_pnl=20)]),
    dict(exit_events=[dict(entry_notional=250, gross_pnl=2.49)]),
    dict(exit_events=[dict(entry_notional=250, gross_pnl=float('nan'))])])
def test_incomplete_or_ineligible_evidence(overrides):
    assert qualify(**overrides) is None


def test_partial_exits_aggregate_only_at_final_close_utc():
    events = [dict(entry_notional=150, gross_pnl=3), dict(entry_notional=150, gross_pnl=0)]
    final = datetime(2026, 11, 18, 1, tzinfo=timezone(timedelta(hours=2)))
    assert qualify(exit_events=events, close_dt=final) == "2026-11-17"
    assert qualify(exit_events=events, is_open=True) is None


def test_profitable_slice_does_not_qualify_a_flat_total_trade():
    assert qualify(exit_events=[dict(entry_notional=250, gross_pnl=10),
                                dict(entry_notional=250, gross_pnl=-10)]) is None
