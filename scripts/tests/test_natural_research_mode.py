"""Offline tests only: no exchange access, data download or backtest workers."""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from scripts import run_natural_benchmark_auto as nat


@pytest.fixture(autouse=True)
def reset_mode(monkeypatch):
    monkeypatch.setattr(nat, "RESEARCH_MODE", False)
    monkeypatch.setattr(nat, "ALLOW_SYNTHETIC", False)
    monkeypatch.setattr(nat, "MCAP_MODE", "strict")


def test_research_flags_are_forwarded_only_explicitly(monkeypatch):
    monkeypatch.setattr(nat, "detail_cache_enabled", lambda: False)
    assert "--allow-research-schedule" not in nat.common_args()
    monkeypatch.setattr(nat, "RESEARCH_MODE", True)
    assert "--allow-research-schedule" in nat.common_args()
    assert "--allow-synthetic-market-data" not in nat.common_args()
    monkeypatch.setattr(nat, "ALLOW_SYNTHETIC", True)
    assert "--allow-synthetic-market-data" in nat.common_args()


@pytest.mark.parametrize("args", [["--mcap-mode", "hybrid"], ["--allow-synthetic-market-data"], ["--workers", "-1"]])
def test_invalid_cli_rejected_before_preparation(monkeypatch, args):
    monkeypatch.setattr(nat, "prepare_data_and_hourly_schedules", lambda: pytest.fail("Preparation started"))
    with pytest.raises(SystemExit) as exc:
        nat.main(args)
    assert exc.value.code == 2


@pytest.mark.parametrize("action", ["--check-only", "--prepare-only"])
def test_cli_reads_sys_argv_and_never_starts_workers_for_checks(tmp_path, monkeypatch, action):
    monkeypatch.setattr(nat, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(nat, "OUT", tmp_path / "evidence")
    monkeypatch.setattr(sys, "argv", ["run_natural_benchmark_auto.py", "--research", "--mcap-mode", "hybrid", action, "--workers", "5"])
    calls = []
    monkeypatch.setattr(nat, "prepare_data_and_hourly_schedules", lambda: calls.append("prepare"))
    monkeypatch.setattr(nat, "run_preflight", lambda: {"issues": []})
    monkeypatch.setattr(nat, "natural_candidates", lambda: pytest.fail("Benchmark reached"))
    nat.main()
    assert nat.RESEARCH_MODE and nat.MCAP_MODE == "hybrid"
    assert calls == (["prepare"] if action == "--prepare-only" else [])


def test_failed_preflight_prevents_workers(tmp_path, monkeypatch):
    monkeypatch.setattr(nat, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(nat, "OUT", tmp_path / "evidence")
    monkeypatch.setattr(nat, "prepare_data_and_hourly_schedules", lambda: None)
    monkeypatch.setattr(nat, "run_preflight", lambda: {"issues": ["missing minute candles"]})
    monkeypatch.setattr(nat, "natural_candidates", lambda: pytest.fail("Benchmark reached"))
    with pytest.raises(SystemExit, match="nessun backtest"):
        nat.main(["--research"])


def candle_file(tmp_path, dates):
    path = tmp_path / "candles.feather"
    pd.DataFrame({"date": dates, "open": 10., "high": 11., "low": 9., "close": 10., "volume": 1.}).to_feather(path)
    return path


@pytest.mark.parametrize("problem", ["gap", "duplicate", "order", "truncated", "nan", "ohlc"])
def test_candle_validation_rejects_corrupt_or_incomplete_data(tmp_path, problem):
    dates = pd.date_range("2025-01-01", periods=4, freq="min", tz="UTC")
    changed = dates
    if problem == "gap":
        changed = dates.delete(1)
    elif problem == "duplicate":
        changed = dates.insert(1, dates[0])
    elif problem == "order":
        changed = dates[::-1]
    elif problem == "truncated":
        changed = dates[:-1]
    path = candle_file(tmp_path, changed)
    if problem in ("nan", "ohlc"):
        frame = pd.read_feather(path)
        frame.loc[0, "high"] = float("nan") if problem == "nan" else 1.
        frame.to_feather(path)
    with pytest.raises(ValueError):
        nat.inspect_candle_file(path, "1m", int(dates[0].timestamp()), int(dates[-1].timestamp()) + 60)


def test_valid_candles_and_sparse_funding(tmp_path):
    dates = pd.date_range("2025-01-01", periods=4, freq="min", tz="UTC")
    path = candle_file(tmp_path, dates)
    assert nat.inspect_candle_file(path, "1m", int(dates[0].timestamp()), int(dates[-1].timestamp()) + 60)["rows"] == 4
    funding_dates = pd.date_range("2025-01-01", periods=4, freq="8h", tz="UTC")
    pd.DataFrame({"date": funding_dates, "open": [-0.001, 0.001, 0., 0.002]}).to_feather(path)
    assert nat.inspect_candle_file(path, "1h", int(funding_dates[0].timestamp()), int(funding_dates[-1].timestamp()), funding=True)["rows"] == 4


def schedule_doc():
    start = int(datetime(2024, 11, 15, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2026, 11, 15, tzinfo=timezone.utc).timestamp())
    return {"resolution": "1h", "zero_lookahead": False, "mcap_mode": "hybrid",
            "pairlist_order": "volume_top_n_then_filters", "anti_survivorship": True,
            "include_archived_delisted": True, "coverage_diagnostics": {"empty_days": 0, "empty_hours": 0},
            "schedule": {str(ts): ["BTC/USDT:USDT"] for ts in range(start, end, 86400)},
            "ever_whitelisted_2y": ["BTC/USDT:USDT"]}


def test_only_research_accepts_unverified_schedule(monkeypatch):
    monkeypatch.setattr(nat, "read_json", lambda path: schedule_doc())
    monkeypatch.setattr(nat, "_archive_issues_and_pairs", lambda: ([], set()))
    monkeypatch.setattr(nat, "_futures_files_issues", lambda *args: [])
    assert any("historical evidence" in issue for issue in nat.hourly_schedule_issues())
    monkeypatch.setattr(nat, "RESEARCH_MODE", True)
    monkeypatch.setattr(nat, "MCAP_MODE", "hybrid")
    assert nat.hourly_schedule_issues() == []


def test_preflight_records_synthetic_warnings_and_blocks_schedule_gaps(tmp_path, monkeypatch):
    monkeypatch.setattr(nat, "OUT", tmp_path)
    monkeypatch.setattr(nat, "ROOT", tmp_path)
    monkeypatch.setattr(nat, "RESEARCH_MODE", True)
    monkeypatch.setattr(nat, "ALLOW_SYNTHETIC", True)
    monkeypatch.setattr(nat, "MCAP_MODE", "hybrid")
    doc = schedule_doc()
    monkeypatch.setattr(nat, "read_json", lambda path: doc)
    monkeypatch.setattr(nat, "hourly_schedule_issues", lambda: [])
    monkeypatch.setattr(nat, "inspect_candle_file", lambda *a, **kw: {"rows": 123})
    monkeypatch.setattr(pd, "read_feather", lambda *a, **kw: pd.DataFrame({"date": pd.date_range("2024-07-18", periods=120, tz="UTC")}))
    result = nat.run_preflight()
    assert result["passed"]
    assert len(result["warnings"]) == 2
    assert result["research"]["live_parity_certified"] is False
    assert result["schedule_evidence"]["base"]["zero_lookahead"] is False
    assert (tmp_path / "natural_preflight.json").is_file()
    doc["schedule"].pop(next(iter(doc["schedule"])))
    assert not nat.run_preflight()["passed"]


def test_mode_changes_invalidate_resume_signature(tmp_path, monkeypatch):
    monkeypatch.setattr(nat, "ROOT", tmp_path)
    path = tmp_path / "input.json"
    path.write_text("{}")
    monkeypatch.setattr(nat, "ENGINE", path)
    original = nat.engine_signature(path, kind="grid", step=7)
    monkeypatch.setattr(nat, "RESEARCH_MODE", True)
    research = nat.engine_signature(path, kind="grid", step=7)
    assert research != original
    monkeypatch.setattr(nat, "ALLOW_SYNTHETIC", True)
    assert nat.engine_signature(path, kind="grid", step=7) != research


def test_research_orchestration_uses_requested_workers_and_labels_report(tmp_path, monkeypatch):
    from scripts.tests.test_natural_benchmark_auto import _mock_natural_row

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(nat, "ROOT", tmp_path)
    monkeypatch.setattr(nat, "OUT", tmp_path / "evidence")
    monkeypatch.setattr(nat, "prepare_data_and_hourly_schedules", lambda: None)
    monkeypatch.setattr(nat, "run_preflight", lambda: {"issues": []})
    monkeypatch.setattr(nat, "natural_candidates", lambda: [{"id": "C1"}])
    calls = []

    def grid(preset, ids, step, workers):
        calls.append((preset, workers))
        return [_mock_natural_row("C1")]

    monkeypatch.setattr(nat, "run_natural_grid", grid)
    monkeypatch.setattr(nat, "run_stress_suite", lambda *args: {})
    monkeypatch.setattr(nat, "run_script", lambda *args: None)
    nat.main(["--research", "--mcap-mode", "hybrid", "--allow-synthetic-market-data", "--workers", "5"])
    assert calls == [(preset, 5) for preset in nat.PRESETS_ORDER]
    summary = nat.read_json(nat.OUT / "natural_benchmark_summary.json")
    assert summary["research"]["mode"] == "research"
    assert summary["research"]["allow_synthetic_market_data"]
    assert not summary["research"]["live_parity_certified"]
    assert "NON CERTIFICATO LIVE" in (nat.OUT / "natural_benchmark_report.txt").read_text(encoding="utf-8")
