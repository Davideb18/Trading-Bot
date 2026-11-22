from datetime import datetime, UTC
from types import SimpleNamespace

import pytest

from scripts.prop_phase_checkpoint import phase_checkpoint


def engine(tmp_path):
    return SimpleNamespace(_phase_checkpoint_root=tmp_path, _prop_profile={"phase": "eval"},
        _prop_policy={"limit": 750}, fee=0.0011, _extra_slippage_bps=10,
        _strict_wick_guard=True, _stop_on_halt=True, _stop_on_pass=True,
        _ignore_target_pause=False, prop_pass_dt=None)


def test_resume_restores_transition_date_and_invalidates_changed_risk(tmp_path):
    bt = engine(tmp_path)
    calls = []

    @phase_checkpoint
    def run_cached_cohort(self, start_date, end_date):
        calls.append(start_date)
        self.prop_pass_dt = datetime(2025, 2, 1, tzinfo=UTC)
        return {"outcome": "PASS", "pass_dt": self.prop_pass_dt.isoformat(), "halt_dt": None}

    expected = run_cached_cohort(bt, "2025-01-01", "2025-04-01")
    bt.prop_pass_dt = None
    assert run_cached_cohort(bt, "2025-01-01", "2025-04-01") == expected
    assert len(calls) == 1
    assert bt.prop_pass_dt == datetime(2025, 2, 1, tzinfo=UTC)
    bt._prop_policy["limit"] = 950
    run_cached_cohort(bt, "2025-01-01", "2025-04-01")
    assert len(calls) == 2
    bt._extra_slippage_bps = 28
    run_cached_cohort(bt, "2025-01-01", "2025-04-01")
    assert len(calls) == 3


def test_failed_phase_is_not_saved(tmp_path):
    @phase_checkpoint
    def run_cached_cohort(self):
        raise RuntimeError("interrupted before phase completion")

    with pytest.raises(RuntimeError):
        run_cached_cohort(engine(tmp_path))
    assert not list(tmp_path.iterdir())


def test_continuous_hit_preserves_profile_and_control_flags(tmp_path):
    bt = engine(tmp_path)
    calls = []

    @phase_checkpoint
    def _run_continuous_pass(bt, profile, ignore_target_pause):
        calls.append(1)
        return {"net_pnl": 12.5, "pass_dt": None, "halt_dt": None}

    expected = _run_continuous_pass(bt, {"phase": "funded"}, True)
    assert _run_continuous_pass(bt, {"phase": "funded"}, True) == expected
    assert calls == [1]
    assert bt._prop_profile == {"phase": "funded"}
    assert (bt._stop_on_halt, bt._stop_on_pass, bt._ignore_target_pause) == (False, False, True)
