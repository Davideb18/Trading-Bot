import pytest

from freqtrade.prop_dca import clamp_addition


def test_non_escalating_and_lifetime_count():
    assert clamp_addition(300, [200]) == 200
    assert clamp_addition(200, [200, 150]) == 150
    assert clamp_addition(100, [200, 150, 100]) is None


def test_single_entry_preserves_reductions():
    assert clamp_addition(100, [250], max_additions=0) is None
    assert clamp_addition(-100, [250], max_additions=0) == -100


@pytest.mark.parametrize("stakes", [[], [0], [float('nan')]])
def test_unknown_initial_allocation_blocks_additions(stakes):
    assert clamp_addition(100, stakes) is None


def test_exchange_minimum_cannot_force_escalation():
    assert clamp_addition(200, [150], min_stake=160) is None
