import copy
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_full_benchmark_auto as auto


def row(cid, slots=3, profit=100):
    result = {"candidate": {"id": cid, "max_open_trades": slots}}
    for window in ("train_1y_funded", "oos_1y_funded", "full_2y_continuous_funded"):
        for suffix in ("", "_two_step"):
            result[window + suffix] = {"net_pnl": profit, "max_overall_dd_wick": 100,
                                       "funded_margin_gt_25pct_bars": 0}
    for group in ("program_comparison", "autumn_2026_daily_crash_sweep"):
        result[group] = {p + "_summary": {"total_cohorts": 57, "fail_official": 0,
                                          "qualify_rate_pct": 80, "first_payout_rate_pct": 50}
                         for p in ("one_step", "two_step")}
    return result


def test_selection_enforces_safety_and_slot_diversity():
    rows = [row(str(i), profit=1000-i) for i in range(5)] + [row("two", 2, 10)]
    bad = row("unsafe", profit=10000)
    bad["autumn_2026_daily_crash_sweep"]["two_step_summary"]["fail_official"] = 1
    rows.append(bad)
    assert set(auto.top_four(rows)) == {"0", "1", "2", "two"}
    bad = copy.deepcopy(rows[0])
    bad["oos_1y_funded_two_step"]["funded_margin_gt_25pct_bars"] = 1
    assert not auto.safe(bad)
    with pytest.raises(RuntimeError, match="Solo"):
        auto.top_four([bad, row("ok")])


def test_incomplete_crash_sweep_cannot_qualify():
    value = row("a")
    value["autumn_2026_daily_crash_sweep"]["one_step_summary"]["total_cohorts"] = 56
    assert not auto.complete_row(value)
    assert not auto.safe(value)


def test_data_readiness_requires_daily_coverage_archives_and_futures(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(auto, "OUT", Path("evidence"))
    buckets = {}
    day = date(2024, 11, 15)
    while day < date(2026, 11, 15):
        buckets[day.isoformat()] = ["BTC/USDT:USDT"]
        day += timedelta(days=1)
    doc = {"schedule": buckets, "coverage_diagnostics": {"empty_days": 0},
           "anti_survivorship": True, "include_archived_delisted": True}
    for preset in ("base", "strict", "bluechip"):
        auto.atomic_json(auto.schedule_path(preset), doc)
    archive = Path("evidence/archive/OLDUSDT/day-1m.feather")
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"fixture")
    auto.atomic_json(Path("evidence/archive_download_manifest.json"), {
        "sample_only": False, "start": "2024-11-01", "end": "2026-11-18",
        "results": [{"symbol": "OLDUSDT", "output": str(archive)}]})
    futures = Path("user_data/data/bybit/futures")
    futures.mkdir(parents=True)
    for symbol in ("BTC", "OLD"):
        for tf in auto.TIMEFRAMES:
            (futures / f"{symbol}_USDT_USDT-{tf}-futures.feather").write_bytes(b"fixture")
    for kind in ("mark", "funding_rate"):
        (futures / f"BTC_USDT_USDT-4h-{kind}.feather").write_bytes(b"fixture")
    assert auto.data_ready()
    monkeypatch.setattr(auto, "run", lambda *args: pytest.fail("Unexpected data acquisition"))
    auto.prepare_data()
    (futures / "OLD_USDT_USDT-1m-futures.feather").unlink()
    assert not auto.data_ready()


def test_checkpoint_resumes_partial_skips_complete_and_invalidates_changes(tmp_path, monkeypatch):
    output = tmp_path / "grid.json"
    monkeypatch.setattr(auto, "signature", lambda *args: "v1")
    calls = []
    auto.atomic_json(output, {"compatible": True, "done": False})

    def run(command):
        calls.append(command)
        assert auto.read_json(output)["compatible"]
        auto.atomic_json(output, {"compatible": True, "done": True})

    monkeypatch.setattr(auto, "run", run)
    args = (output, ["fake"], Path("schedule"), lambda d: d.get("done"), lambda d: d.get("compatible"))
    auto.checkpoint_job(*args)
    auto.checkpoint_job(*args)
    assert len(calls) == 1
    monkeypatch.setattr(auto, "signature", lambda *args: "v2")
    monkeypatch.setattr(auto, "run", lambda command: auto.atomic_json(output, {"compatible": True, "done": True}))
    auto.checkpoint_job(*args)
    assert output.with_suffix(".json.previous").exists()


def test_orchestration_uses_required_flags_and_separate_winners(monkeypatch, tmp_path):
    monkeypatch.setattr(auto, "ROOT", tmp_path)
    monkeypatch.setattr(auto, "OUT", tmp_path)
    monkeypatch.setattr(auto, "prepare_data", lambda: None)
    monkeypatch.setattr(auto, "candidates", lambda: [{"id": str(i)} for i in range(24)])
    commands = []

    def job(output, command, schedule, validate, compatible):
        commands.append(command)
        if "--grid-json" in command:
            rows = [row(str(i), 2 if i == 0 else 3, 100-i) for i in range(4)]
            rows[1]["oos_1y_funded_two_step"]["net_pnl"] = 1000
            return {"candidates": rows}
        return {}

    monkeypatch.setattr(auto, "checkpoint_job", job)
    monkeypatch.setattr(auto, "run_script", lambda *args: commands.append(list(args)))
    monkeypatch.chdir(tmp_path)
    auto.main()
    assert len(commands) == 7  # A, three B, two stress suites, shock bounds.
    for command in commands[:-1]:
        assert "--phase-checkpoint-dir" not in command
        assert "--skip-crash-sweep" not in command
        assert command[command.index("--timeframe-detail") + 1] == "1m"
        assert command[command.index("--ram-profile") + 1] == "auto"
    assert commands[0][commands[0].index("--cohort-step-days") + 1] == "14"
    assert all(c[c.index("--cohort-step-days") + 1] == "7" for c in commands[1:-1])
    assert commands[-1] == ["prop_shock_bounds.py"]
    assert json.loads((tmp_path / "benchmark_selection.json").read_text())["winners"]["two_step"]["candidate"] == "1"


def test_data_readiness_accepts_unix_epoch_schedule_keys(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(auto, "OUT", Path("evidence"))
    buckets = {}
    day = date(2024, 11, 15)
    while day < date(2026, 11, 15):
        ts = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
        buckets[str(ts)] = ["BTC/USDT:USDT"]
        day += timedelta(days=1)
    doc = {"schedule": buckets, "coverage_diagnostics": {"empty_days": 0},
           "anti_survivorship": True, "include_archived_delisted": True}
    for preset in ("base", "strict", "bluechip"):
        auto.atomic_json(auto.schedule_path(preset), doc)
    archive = Path("evidence/archive/OLDUSDT/day-1m.feather")
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"fixture")
    auto.atomic_json(Path("evidence/archive_download_manifest.json"), {
        "sample_only": False, "start": "2024-11-01", "end": "2026-11-18",
        "results": [{"symbol": "OLDUSDT", "output": str(archive)}]})
    futures = Path("user_data/data/bybit/futures")
    futures.mkdir(parents=True)
    for symbol in ("BTC", "OLD"):
        for tf in auto.TIMEFRAMES:
            (futures / f"{symbol}_USDT_USDT-{tf}-futures.feather").write_bytes(b"fixture")
    for kind in ("mark", "funding_rate"):
        (futures / f"BTC_USDT_USDT-1h-{kind}.feather").write_bytes(b"fixture")
    assert auto.data_readiness_issues() == []
    assert auto.data_ready()


def test_prepare_data_uses_demo_trading_override_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(auto, "OUT", tmp_path / "evidence")
    ready_calls = [0]

    def fake_issues():
        ready_calls[0] += 1
        return ["missing"] if ready_calls[0] == 1 else []

    monkeypatch.setattr(auto, "data_readiness_issues", fake_issues)
    seen_override = {}
    seen_commands = []

    def fake_run(command):
        seen_commands.append(command)
        if "download-data" in command:
            config_indices = [i for i, arg in enumerate(command) if arg == "--config"]
            assert len(config_indices) == 2
            override = Path(command[config_indices[1] + 1])
            assert override.is_file()
            seen_override.update(json.loads(override.read_text(encoding="utf-8")))

    monkeypatch.setattr(auto, "run", fake_run)
    auto.prepare_data()
    assert seen_override == {"dry_run": True, "exchange": {"demo_trading": False, "key": "", "secret": ""}}
    assert not (tmp_path / "evidence" / "_download_override.json").exists()
    assert any("download-data" in cmd for cmd in seen_commands)


def test_detail_cache_flag_adapts_to_free_disk_space(tmp_path, monkeypatch):
    monkeypatch.setattr(auto, "ROOT", tmp_path)
    monkeypatch.setattr(auto, "DETAIL_CACHE_DIR", Path("detail_mmap_cache"))
    monkeypatch.setattr(auto.shutil, "disk_usage", lambda _: SimpleNamespace(free=7 * 1024**3))
    assert "--detail-array-cache" not in auto.common_args()
    monkeypatch.setattr(auto.shutil, "disk_usage", lambda _: SimpleNamespace(free=40 * 1024**3))
    assert "--detail-array-cache" in auto.common_args()
