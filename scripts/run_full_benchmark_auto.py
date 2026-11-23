"""Offline benchmark orchestrator. Run again after interruption to resume checkpoints.

Only the standard library is imported here; subprocesses use the active Python
environment with Freqtrade installed. No trading, deployment or live credentials
are changed. Ranking uses equal-weight OOS/24-month return-to-drawdown, followed
by qualification and payout rates; official breaches always disqualify a row.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from datetime import date, timedelta

ROOT = Path(__file__).resolve().parents[1]
OUT = Path("user_data/forensic/strict_validation")
ENGINE = Path("scripts/prop_backtest_1to1.py")
TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1d")
SCENARIOS = {"base_0bps", "friction_10bps_rt", "stress_28bps_rt",
             "strict_wick_guard", "stress_28bps_strict_wick"}
COMMON = ["--allow-research-schedule", "--start", "2024-11-15", "--end", "2026-11-15",
          "--timeframe-detail", "1m", "--ram-profile", "auto",
          "--detail-array-cache", "user_data/forensic/detail_mmap_cache"]


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")
    temp.replace(path)


def candidates():
    # Read the literal grid without importing the backtesting engine.
    tree = ast.parse(ENGINE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "DEFAULT_MONEY_GRID":
            grid = ast.literal_eval(node.value)
            if len(grid) != 24 or len({c["id"] for c in grid}) != 24:
                raise RuntimeError("Expected exactly 24 distinct benchmark candidates.")
            return grid
    raise RuntimeError("DEFAULT_MONEY_GRID missing.")


def schedule_path(preset):
    return OUT / f"pairlist_schedule_{preset}_2y.json"


def present(path):
    return path.is_file() and path.stat().st_size > 0


def data_ready():
    """Check every daily bucket, required futures file and archived download output."""
    pairs = set()
    for preset in ("base", "strict", "bluechip"):
        doc = read_json(schedule_path(preset))
        if not isinstance(doc, dict):
            return False
        if doc.get("coverage_diagnostics", {}).get("empty_days") != 0:
            return False
        if not doc.get("anti_survivorship") or not doc.get("include_archived_delisted"):
            return False
        buckets = {key[:10]: value for key, value in doc.get("schedule", {}).items()}
        day = date(2024, 11, 15)
        while day < date(2026, 11, 15):
            values = buckets.get(day.isoformat())
            if not values:
                return False
            pairs.update(values)
            day += timedelta(days=1)
        pairs.update(doc.get("ever_whitelisted_2y", []))
    archive = OUT / "archive"
    manifest = read_json(OUT / "archive_download_manifest.json")
    if not isinstance(manifest, dict) or manifest.get("sample_only", True):
        return False
    if manifest.get("start", "9999") > "2024-11-01" or manifest.get("end", "") < "2026-11-18":
        return False
    records = manifest.get("results", [])
    if not records:
        return False
    archived_pairs = set()
    for record in records:
        # Resolve the local archive layout even when a manifest came from another OS.
        filename = str(record.get("output", "")).replace("\\", "/").rsplit("/", 1)[-1]
        if not filename or not present(archive / record["symbol"] / filename):
            return False
        archived_pairs.add(record["symbol"][:-4] + "/USDT:USDT")
    pairs.update(archived_pairs)
    futures = Path("user_data/data/bybit/futures")
    for pair in pairs:
        prefix = pair.replace("/", "_").replace(":", "_")
        if any(not present(futures / f"{prefix}-{tf}-futures.feather") for tf in TIMEFRAMES):
            return False
        # The engine records explicit in-memory fallbacks for archived delistings.
        # Active instruments must have independent mark and funding data on disk.
        if pair not in archived_pairs:
            for kind in ("mark", "funding_rate"):
                if not any(present(p) for p in futures.glob(f"{prefix}-*-{kind}.feather")):
                    return False
    return bool(pairs)


def run(command):
    print("\n>>> " + subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def run_script(name, *args):
    run([sys.executable, "-u", "scripts/" + name, *map(str, args)])


def prepare_data():
    if data_ready():
        print("Dati e tre schedule presenti: acquisizione saltata.", flush=True)
        return
    pairs = str(OUT / "eligible_bybit_pairs.json")
    run_script("prop_prepare_benchmark_data.py", "--anti-survivorship", "--export-download-pairs", pairs)
    run([sys.executable, "-m", "freqtrade", "download-data", "--config",
         "user_data/config_prop_nfix8.json", "--pairs-file", pairs, "--trading-mode", "futures",
         "--timeframes", *TIMEFRAMES, "--timerange", "20241101-20261118", "--prepend"])
    run_script("prop_archive_data.py", "--start", "2024-11-01", "--end", "2026-11-18", "--delete-raw")
    run_script("prop_marketcap_archive.py")
    run_script("prop_prepare_benchmark_data.py", "--start", "2024-11-15", "--end", "2026-11-15",
               "--anti-survivorship", "--include-archived-delisted", "--mcap-mode", "hybrid",
               "--fetch-coingecko", "--build-presets-dir", OUT)
    if not data_ready():
        raise RuntimeError("Dati/schedule ancora incompleti: nessun backtest avviato. Correggere i dati e rilanciare.")


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def complete_row(row):
    try:
        for window in ("train_1y_funded", "oos_1y_funded", "full_2y_continuous_funded"):
            for suffix in ("", "_two_step"):
                for key in ("net_pnl", "max_overall_dd_wick", "funded_margin_gt_25pct_bars"):
                    if not math.isfinite(float(row[window + suffix][key])):
                        return False
        for group in ("program_comparison", "autumn_2026_daily_crash_sweep"):
            for program in ("one_step", "two_step"):
                summary = row[group][program + "_summary"]
                if summary["total_cohorts"] <= 0 or "fail_official" not in summary:
                    return False
                if group.startswith("autumn") and summary["total_cohorts"] != 57:
                    return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


def safe(row):
    if not complete_row(row):
        return False
    for item in walk(row):
        for key, value in item.items():
            if key in ("fail_official", "funded_margin_gt_25pct_bars", "funded_notional_gt_2x_bars",
                       "official_daily_breach_wick_bars", "official_overall_breach_wick_bars",
                       "total_official_daily_breach_wick_bars") and value != 0:
                return False
            if key in ("official_breach_close", "official_breach_wick") and value is not None:
                return False
            if isinstance(value, str) and value == "FAIL_OFFICIAL":
                return False
    return True


def score(row, program=None):
    programs = [program] if program else ["one_step", "two_step"]
    returns, rates, payouts = [], [], []
    for name in programs:
        suffix = "_two_step" if name == "two_step" else ""
        for window in ("oos_1y_funded", "full_2y_continuous_funded"):
            stats = row[window + suffix]
            returns.append(stats["net_pnl"] / max(1.0, stats["max_overall_dd_wick"]))
        for group in ("program_comparison", "autumn_2026_daily_crash_sweep"):
            summary = row[group][name + "_summary"]
            rates.append(summary["qualify_rate_pct"])
            payouts.append(summary["first_payout_rate_pct"])
    return (sum(returns) / len(returns), min(rates), sum(payouts) / len(payouts))


def top_four(rows):
    eligible = sorted((r for r in rows if safe(r)), key=lambda r: (score(r), r["candidate"]["id"]), reverse=True)
    if len(eligible) < 4:
        raise RuntimeError(f"Solo {len(eligible)} candidati sicuri: impossibile selezionare Top 4 senza allentare i vincoli.")
    chosen = []
    for slots in (2, 3):
        match = next((r for r in eligible if r["candidate"]["max_open_trades"] == slots), None)
        if match is not None:
            chosen.append(match)
    for row in eligible:
        if row not in chosen and len(chosen) < 4:
            chosen.append(row)
    return [r["candidate"]["id"] for r in chosen]


def signature(command, schedule):
    digest = hashlib.sha256(json.dumps(command).encode())
    for path in (ENGINE, Path(__file__), schedule):
        digest.update(path.read_bytes())
    return digest.hexdigest()


def checkpoint_job(output, command, schedule, validate, compatible):
    """Preserve incompatible artifacts; let the engine resume compatible partial JSON."""
    marker = output.with_suffix(output.suffix + ".run.json")
    identity = signature(command, schedule)
    doc = read_json(output)
    old = read_json(marker)
    matches = compatible(doc) and (not old or old.get("signature") == identity)
    if present(output) and not matches:
        backup = output.with_suffix(output.suffix + ".previous")
        index = 1
        while backup.exists():
            backup = output.with_suffix(output.suffix + f".previous.{index}")
            index += 1
        output.replace(backup)
        doc = {}
    atomic_json(marker, {"signature": identity, "command": command})
    if matches and validate(doc):
        print(f"Completo: {output.name}; saltato.", flush=True)
        return doc
    run(command)
    result = read_json(output)
    if not compatible(result) or not validate(result):
        raise RuntimeError(f"Output incompleto o incompatibile: {output}")
    return result


def grid(preset, ids, step, name):
    schedule = schedule_path(preset)
    command = [sys.executable, "-u", str(ENGINE), *COMMON, "--pairlist-schedule", str(schedule),
               "--cohort-step-days", str(step), "--slippage-bps", "10", "--strict-wick-guard",
               "--candidates", *ids, "--grid-json", str(OUT / name)]

    def compatible(doc):
        return (isinstance(doc, dict) and doc.get("schema_version") == 4
                and doc.get("cohort_step_days") == step and doc.get("slippage_bps_roundtrip") == 10
                and doc.get("strict_wick_guard") is True
                and doc.get("train_window", [""])[0][:10] == "2024-11-15"
                and doc.get("oos_window", ["", ""])[-1][:10] == "2026-11-15"
                and doc.get("schedule_meta", {}).get("path") == str(schedule)
                and all(complete_row(r) for r in doc.get("candidates", [])))

    return checkpoint_job(OUT / name, command, schedule,
                          lambda d: {r["candidate"]["id"] for r in d.get("candidates", [])} == set(ids),
                          compatible)["candidates"]


def stress(preset, candidate, name):
    schedule = schedule_path(preset)
    command = [sys.executable, "-u", str(ENGINE), *COMMON, "--pairlist-schedule", str(schedule),
               "--cohort-step-days", "7", "--slippage-bps", "10", "--strict-wick-guard",
               "--candidates", candidate, "--suite-json", str(OUT / name)]

    def compatible(doc):
        if not isinstance(doc, dict):
            return False
        meta = doc.get("meta", {})
        return (meta.get("schema_version") == 3 and meta.get("timeframe_detail") == "1m"
                and meta.get("cohort_step_days") == 7
                and meta.get("start", "")[:10] == "2024-11-15"
                and meta.get("end", "")[:10] == "2026-11-15"
                and (meta.get("applied_candidate") or {}).get("id") == candidate
                and meta.get("schedule_meta", {}).get("path") == str(schedule))

    def complete(doc):
        scenarios = doc.get("scenarios", {})
        if not SCENARIOS <= scenarios.keys():
            return False
        for result in scenarios.values():
            # Reuse structural checks without requiring train/OOS in stress outputs.
            row = dict(result)
            for window in ("train_1y_funded", "oos_1y_funded", "full_2y_continuous_funded"):
                row[window] = result.get("continuous_funded", {})
                row[window + "_two_step"] = result.get("continuous_funded_two_step", {})
            if not complete_row(row):
                return False
        return True

    checkpoint_job(OUT / name, command, schedule, complete, compatible)


def main():
    os.chdir(ROOT)
    OUT.mkdir(parents=True, exist_ok=True)
    all_ids = [c["id"] for c in candidates()]
    prepare_data()
    rows = grid("base", all_ids, 14, "grid_base_24cands.json")
    selected = top_four(rows)
    print("Top 4: " + ", ".join(selected), flush=True)
    options = []
    for preset in ("base", "strict", "bluechip"):
        rows = grid(preset, selected, 7, f"grid_top4_{preset}_7d_crash.json")
        options.extend((preset, row) for row in rows if safe(row))
    if not options:
        raise RuntimeError("Test B: nessun vincitore rispetta i vincoli di sicurezza.")
    winners = {}
    for program in ("one_step", "two_step"):
        preset, row = max(options, key=lambda item: (score(item[1], program), item[0], item[1]["candidate"]["id"]))
        winners[program] = {"preset": preset, "candidate": row["candidate"]["id"], "score": score(row, program)}
    atomic_json(OUT / "benchmark_selection.json", {"top4": selected, "winners": winners,
                "ranking": "equal OOS/24m net_pnl/max(1,wick drawdown), then qualification and payout rates"})
    first = winners["one_step"]
    stress(first["preset"], first["candidate"], "suite_winner_stress.json")
    second = winners["two_step"]
    if (second["preset"], second["candidate"]) != (first["preset"], first["candidate"]):
        stress(second["preset"], second["candidate"], "suite_winner_2step_stress.json")
    run_script("prop_shock_bounds.py")
    print("=== TUTTO COMPLETATO! ORA INCOLLA IL PROMPT 2 IN CODEX ===", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError, KeyboardInterrupt) as exc:
        print(f"\nInterrotto: {exc}\nRilanciare lo stesso comando per riprendere.", file=sys.stderr)
        sys.exit(1)
