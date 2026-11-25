"""Offline benchmark orchestrator. Run again after interruption to resume checkpoints.

Only the standard library is imported here; subprocesses use the active Python
environment with Freqtrade installed. No trading, deployment or live credentials
are changed. Ranking uses equal-weight OOS/24-month return-to-drawdown, followed
by qualification and payout rates; official breaches always disqualify a row.
"""

from __future__ import annotations

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[1]
OUT = Path("user_data/forensic/strict_validation")
ENGINE = Path("scripts/prop_backtest_1to1.py")
TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1d")
SCENARIOS = {"base_0bps", "friction_10bps_rt", "stress_28bps_rt",
             "strict_wick_guard", "stress_28bps_strict_wick"}
DETAIL_CACHE_DIR = Path("user_data/forensic/detail_mmap_cache")
DETAIL_CACHE_MIN_FREE_BYTES = 25 * 1024**3
COMMON = ["--allow-research-schedule", "--start", "2024-11-15", "--end", "2026-11-15",
          "--timeframe-detail", "1m", "--ram-profile", "auto"]
HEARTBEAT_SEC = 300.0
WORKER_STAGGER_SEC = 4.0


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _replace_atomic(src, dst):
    for attempt in range(5):
        try:
            src.replace(dst)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.1 * (attempt + 1))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")
    _replace_atomic(temp, path)


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


def detect_total_ram_gb():
    try:
        if sys.platform == "win32":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                    (name, ctypes.c_ulonglong) for name in (
                        "total_phys", "avail_phys", "total_page", "avail_page",
                        "total_virtual", "avail_virtual", "avail_extended",
                    )
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.total_phys / (1024.0 ** 3)
        if sys.platform == "darwin":
            return int(subprocess.check_output(["sysctl", "-n", "hw.memsize"])) / (1024.0 ** 3)
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return (pages * page_size) / (1024.0 ** 3)
    except Exception:
        pass
    return 8.0


def choose_workers(ram_gb=None, cpu_count=None):
    """Pick safe parallel worker count from physical RAM (~1.8 GB/worker in normal profile)."""
    ram = detect_total_ram_gb() if ram_gb is None else float(ram_gb)
    cpus = (os.cpu_count() or 1) if cpu_count is None else int(cpu_count)
    if ram < 12.0:
        return 1
    if ram < 22.0:
        return max(1, min(4, cpus // 2))
    if ram < 40.0:
        return max(1, min(6, cpus // 2))
    return max(1, min(8, cpus // 2))


def detail_cache_enabled():
    """Enable disk mmap 1m cache only when already populated or >= 25 GiB free."""
    cache_dir = DETAIL_CACHE_DIR if DETAIL_CACHE_DIR.is_absolute() else ROOT / DETAIL_CACHE_DIR
    if cache_dir.is_dir() and any(cache_dir.rglob("*.complete")):
        return True
    try:
        return shutil.disk_usage(ROOT).free >= DETAIL_CACHE_MIN_FREE_BYTES
    except OSError:
        return False


def common_args(ram_profile="auto"):
    args = list(COMMON)
    if ram_profile != "auto" and "--ram-profile" in args:
        idx = args.index("--ram-profile")
        args[idx + 1] = str(ram_profile)
    if detail_cache_enabled():
        args.extend(["--detail-array-cache", str(DETAIL_CACHE_DIR)])
    return args


def _schedule_day_key(raw_key):
    text = str(raw_key).strip()
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        return text[:10]
    try:
        ts = int(float(text))
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OverflowError, OSError):
        return text[:10]


def data_readiness_issues():
    """Return concrete missing/invalid items blocking a clean 1:1 benchmark run."""
    issues = []
    pairs = set()
    for preset in ("base", "strict", "bluechip"):
        spath = schedule_path(preset)
        doc = read_json(spath)
        if not isinstance(doc, dict) or not doc:
            issues.append(f"schedule mancante o illeggibile: {spath}")
            continue
        if doc.get("coverage_diagnostics", {}).get("empty_days") != 0:
            issues.append(f"schedule {preset}: empty_days != 0")
            continue
        if not doc.get("anti_survivorship") or not doc.get("include_archived_delisted"):
            issues.append(f"schedule {preset}: mancano flag anti_survivorship/include_archived_delisted")
            continue
        buckets = {}
        for key, value in doc.get("schedule", {}).items():
            if value:
                buckets.setdefault(_schedule_day_key(key), []).extend(value)
        missing_days = 0
        day = date(2024, 11, 15)
        while day < date(2026, 11, 15):
            values = buckets.get(day.isoformat())
            if not values:
                missing_days += 1
            else:
                pairs.update(values)
            day += timedelta(days=1)
        if missing_days:
            issues.append(f"schedule {preset}: {missing_days} giorni senza coppie")
        pairs.update(doc.get("ever_whitelisted_2y", []))
    archive = OUT / "archive"
    manifest = read_json(OUT / "archive_download_manifest.json")
    if not isinstance(manifest, dict) or manifest.get("sample_only", True):
        issues.append("archive_download_manifest.json mancante o sample_only")
    elif manifest.get("start", "9999") > "2024-11-01" or manifest.get("end", "") < "2026-11-18":
        issues.append(
            f"archive_download_manifest.json intervallo incompleto ({manifest.get('start')}..{manifest.get('end')})"
        )
    records = manifest.get("results", []) if isinstance(manifest, dict) else []
    if not records:
        issues.append("archive_download_manifest.json senza risultati")
    archived_pairs = set()
    missing_archive = []
    for record in records:
        # Resolve the local archive layout even when a manifest came from another OS.
        filename = str(record.get("output", "")).replace("\\", "/").rsplit("/", 1)[-1]
        if not filename or not present(archive / record["symbol"] / filename):
            missing_archive.append(f"{record.get('symbol')}/{filename}")
            continue
        archived_pairs.add(record["symbol"][:-4] + "/USDT:USDT")
    if missing_archive:
        issues.append(f"file archive delisted mancanti ({len(missing_archive)}, es. {missing_archive[0]})")
    pairs.update(archived_pairs)
    futures = Path("user_data/data/bybit/futures")
    missing_ohlcv = []
    missing_mark_funding = []
    for pair in sorted(pairs):
        prefix = pair.replace("/", "_").replace(":", "_")
        for tf in TIMEFRAMES:
            if not present(futures / f"{prefix}-{tf}-futures.feather"):
                missing_ohlcv.append(f"{prefix}-{tf}")
        # The engine records explicit in-memory fallbacks for archived delistings.
        # Active instruments must have independent mark and funding data on disk.
        if pair not in archived_pairs:
            for kind in ("mark", "funding_rate"):
                if not any(present(p) for p in futures.glob(f"{prefix}-*-{kind}.feather")):
                    missing_mark_funding.append(f"{prefix}-{kind}")
    if missing_ohlcv:
        issues.append(f"candele futures mancanti ({len(missing_ohlcv)}, es. {', '.join(missing_ohlcv[:5])})")
    if missing_mark_funding:
        issues.append(
            f"mark/funding_rate mancanti ({len(missing_mark_funding)}, es. {', '.join(missing_mark_funding[:5])})"
        )
    if not pairs:
        issues.append("nessuna coppia trovata nelle schedule")
    return issues


def data_ready():
    """Check every daily bucket, required futures file and archived download output."""
    return not data_readiness_issues()


def run(command):
    print("\n>>> " + subprocess.list2cmdline(command), flush=True)
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def run_script(name, *args):
    run([sys.executable, "-u", "scripts/" + name, *map(str, args)])


def prepare_data():
    if data_ready():
        print("Dati e tre schedule presenti: acquisizione saltata.", flush=True)
        return
    pairs = str(OUT / "eligible_bybit_pairs.json")
    run_script("prop_prepare_benchmark_data.py", "--anti-survivorship", "--export-download-pairs", pairs)
    # config_prop_nfix8.json enables demo_trading for live Bybit demo execution, which conflicts
    # with Freqtrade download-data (which forces dry_run=True). Pass a temporary override config
    # without modifying user_data/config_prop_nfix8.json.
    override_path = OUT / "_download_override.json"
    atomic_json(
        override_path,
        {"dry_run": True, "exchange": {"demo_trading": False, "key": "", "secret": ""}},
    )
    try:
        run([sys.executable, "-m", "freqtrade", "download-data", "--config",
             "user_data/config_prop_nfix8.json", "--config", str(override_path),
             "--pairs-file", pairs, "--trading-mode", "futures",
             "--timeframes", *TIMEFRAMES, "--timerange", "20241101-20261118", "--prepend"])
    finally:
        override_path.unlink(missing_ok=True)
    run_script("prop_archive_data.py", "--start", "2024-11-01", "--end", "2026-11-18", "--delete-raw")
    run_script("prop_marketcap_archive.py")
    run_script("prop_prepare_benchmark_data.py", "--start", "2024-11-15", "--end", "2026-11-15",
               "--anti-survivorship", "--include-archived-delisted", "--mcap-mode", "hybrid",
               "--fetch-coingecko", "--build-presets-dir", OUT)
    issues = data_readiness_issues()
    if issues:
        detail = "; ".join(issues[:6])
        raise RuntimeError(
            f"Dati/schedule ancora incompleti: nessun backtest avviato ({detail}). Correggere i dati e rilanciare."
        )


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
                       "total_official_daily_breach_wick_bars",
                       "total_official_overall_breach_wick_bars") and value != 0:
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


def engine_signature(schedule, *, kind, step, slippage=10, strict_wick=True, candidate=None):
    """Hash only the engine, schedule, and simulation parameters (orchestrator/worker-count agnostic)."""
    payload = {
        "kind": kind,
        "start": "2024-11-15",
        "end": "2026-11-15",
        "timeframe_detail": "1m",
        "step": int(step),
        "slippage": int(slippage),
        "strict_wick": bool(strict_wick),
        "candidate": candidate,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode())
    for path in (ENGINE, schedule):
        digest.update(Path(path).read_bytes())
    return digest.hexdigest()


def _legacy_marker_compatible(old_marker, *, schedule, step):
    """Allow seamless resume of Marco's completed single-worker checkpoints after orchestrator update."""
    if not isinstance(old_marker, dict) or old_marker.get("engine_signature"):
        return False
    cmd = old_marker.get("command")
    if not isinstance(cmd, list) or not cmd:
        return False
    try:
        if cmd[cmd.index("--cohort-step-days") + 1] != str(step):
            return False
        if cmd[cmd.index("--slippage-bps") + 1] != "10":
            return False
        if "--strict-wick-guard" not in cmd:
            return False
        sched_arg = cmd[cmd.index("--pairlist-schedule") + 1]
        return str(sched_arg).replace("\\", "/") == str(schedule).replace("\\", "/")
    except (ValueError, IndexError):
        return False


def _backup_incompatible(output):
    backup = output.with_suffix(output.suffix + ".previous")
    index = 1
    while backup.exists():
        backup = output.with_suffix(output.suffix + f".previous.{index}")
        index += 1
    _replace_atomic(output, backup)


def _has_populated_indicator_cache():
    cache_root = ROOT / ".backtest_cache"
    return cache_root.is_dir() and any(cache_root.rglob("_manifest.json"))


def _worker_env():
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_logged_worker(command, log_path, active_procs, procs_lock):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        log_file.write(">>> " + subprocess.list2cmdline(command) + "\n")
        log_file.flush()
        proc = subprocess.Popen(
            command,
            cwd=ROOT,
            env=_worker_env(),
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        with procs_lock:
            active_procs.add(proc)
        try:
            rc = proc.wait()
        finally:
            with procs_lock:
                active_procs.discard(proc)
    if rc != 0:
        tail = ""
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = "\n".join(lines[-25:])
        except OSError:
            pass
        raise RuntimeError(
            f"Worker fallito (exit={rc}, log={log_path}):\n{tail}"
        )


def _terminate_procs(active_procs, procs_lock):
    with procs_lock:
        procs = list(active_procs)
    for proc in procs:
        try:
            proc.terminate()
        except OSError:
            pass
    deadline = time.monotonic() + 5.0
    for proc in procs:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except Exception:
            try:
                proc.kill()
            except OSError:
                pass


def checkpoint_job(output, command, schedule, validate, compatible):
    """Preserve incompatible artifacts; let the engine resume compatible partial JSON."""
    marker = output.with_suffix(output.suffix + ".run.json")
    identity = signature(command, schedule)
    doc = read_json(output)
    old = read_json(marker)
    matches = compatible(doc) and (not old or old.get("signature") == identity)
    if present(output) and not matches:
        _backup_incompatible(output)
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


def _summarize_completed_row(row):
    try:
        pnl = float(row["full_2y_continuous_funded"]["net_pnl"])
        dd = float(row["full_2y_continuous_funded"]["max_overall_dd_wick"])
        os_s = row["program_comparison"]["one_step_summary"]
        ts_s = row["program_comparison"]["two_step_summary"]
        fail = int(os_s.get("fail_official", 0)) + int(ts_s.get("fail_official", 0))
        return (
            f"2y Net {pnl:+.2f}$ | DD {dd:.2f}$ | "
            f"1-Step Qual {os_s['qualify_rate_pct']}% | 2-Step Qual {ts_s['qualify_rate_pct']}% | "
            f"Fail {fail}"
        )
    except (KeyError, TypeError, ValueError):
        return "ok"


def grid(preset, ids, step, name, workers=1):
    schedule = schedule_path(preset)
    output = OUT / name
    workers = max(1, int(workers))

    def compatible(doc):
        return (isinstance(doc, dict) and doc.get("schema_version") == 4
                and doc.get("cohort_step_days") == step and doc.get("slippage_bps_roundtrip") == 10
                and doc.get("strict_wick_guard") is True
                and doc.get("train_window", [""])[0][:10] == "2024-11-15"
                and doc.get("oos_window", ["", ""])[-1][:10] == "2026-11-15"
                and str(doc.get("schedule_meta", {}).get("path") or "").replace("\\", "/")
                == str(schedule).replace("\\", "/")
                and all(complete_row(r) for r in doc.get("candidates", [])))

    if workers <= 1:
        command = [sys.executable, "-u", str(ENGINE), *common_args("auto"), "--pairlist-schedule", str(schedule),
                   "--cohort-step-days", str(step), "--slippage-bps", "10", "--strict-wick-guard",
                   "--candidates", *ids, "--grid-json", str(output)]
        return checkpoint_job(output, command, schedule,
                              lambda d: {r["candidate"]["id"] for r in d.get("candidates", [])} == set(ids),
                              compatible)["candidates"]

    marker = output.with_suffix(output.suffix + ".run.json")
    eng_sig = engine_signature(schedule, kind="grid", step=step, slippage=10, strict_wick=True)
    doc = read_json(output)
    old = read_json(marker)
    marker_ok = (
        not old
        or old.get("engine_signature") == eng_sig
        or _legacy_marker_compatible(old, schedule=schedule, step=step)
    )
    matches = compatible(doc) and marker_ok
    if present(output) and not matches:
        _backup_incompatible(output)
        doc = {}

    completed_by_id = {}
    meta_template = {}
    if matches:
        meta_template = {k: v for k, v in doc.items() if k != "candidates"}
        for row in doc.get("candidates", []):
            cid = (row.get("candidate") or {}).get("id")
            if cid in ids and complete_row(row):
                completed_by_id[cid] = row

    workers_dir = OUT / "_workers"
    # Harvest any finished single-candidate worker outputs left by a previous interrupted run.
    if workers_dir.is_dir():
        for cid in ids:
            if cid in completed_by_id:
                continue
            wpath = workers_dir / f"{output.stem}.{cid}.json"
            if not present(wpath):
                continue
            wdoc = read_json(wpath)
            if compatible(wdoc):
                for row in wdoc.get("candidates", []):
                    if (row.get("candidate") or {}).get("id") == cid and complete_row(row):
                        completed_by_id[cid] = row
                        if not meta_template:
                            meta_template = {k: v for k, v in wdoc.items() if k != "candidates"}
                        break
            wpath.unlink(missing_ok=True)

    atomic_json(marker, {"engine_signature": eng_sig, "workers": workers, "preset": preset, "step": step})

    def _persist_merged():
        ordered = [completed_by_id[cid] for cid in ids if cid in completed_by_id]
        if not ordered or not meta_template:
            return
        payload = dict(meta_template)
        payload["candidates"] = ordered
        atomic_json(output, payload)

    if completed_by_id:
        _persist_merged()

    remaining = [cid for cid in ids if cid not in completed_by_id]
    if not remaining:
        print(f"Completo: {output.name} ({len(completed_by_id)}/{len(ids)}); saltato.", flush=True)
        return [completed_by_id[cid] for cid in ids]

    eff_workers = min(workers, len(remaining))
    print(
        f"\n=== PARALLEL GRID {output.name}: {len(completed_by_id)}/{len(ids)} gia completati, "
        f"{len(remaining)} rimanenti con {eff_workers} worker paralleli (ram-profile=normal) ===",
        flush=True,
    )

    merge_lock = threading.Lock()
    procs_lock = threading.Lock()
    active_procs = set()
    running_started = {}
    stop_heartbeat = threading.Event()
    cancel_event = threading.Event()

    def _run_one_candidate(cid, stagger_sec=0.0):
        nonlocal meta_template
        if stagger_sec > 0 and cancel_event.wait(stagger_sec):
            raise RuntimeError(f"Annullato prima dell'avvio di {cid}")
        if cancel_event.is_set():
            raise RuntimeError(f"Annullato prima dell'avvio di {cid}")
        wjson = workers_dir / f"{output.stem}.{cid}.json"
        wlog = workers_dir / f"{output.stem}.{cid}.log"
        command = [
            sys.executable, "-u", str(ENGINE), *common_args("normal"),
            "--pairlist-schedule", str(schedule),
            "--cohort-step-days", str(step),
            "--slippage-bps", "10", "--strict-wick-guard",
            "--candidates", cid, "--grid-json", str(wjson),
        ]
        t0 = time.monotonic()
        with merge_lock:
            running_started[cid] = t0
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] Avviato: {cid} (log: {wlog})", flush=True)
        last_exc = None
        for attempt in range(2):
            if cancel_event.is_set():
                break
            try:
                _run_logged_worker(command, wlog, active_procs, procs_lock)
                wdoc = read_json(wjson)
                if not compatible(wdoc):
                    raise RuntimeError(f"Output worker incompatibile per {cid}: {wjson}")
                row = next(
                    (r for r in wdoc.get("candidates", []) if (r.get("candidate") or {}).get("id") == cid),
                    None,
                )
                if row is None or not complete_row(row):
                    raise RuntimeError(f"Riga worker incompleta per {cid}: {wjson}")
                elapsed_min = (time.monotonic() - t0) / 60.0
                with merge_lock:
                    running_started.pop(cid, None)
                    completed_by_id[cid] = row
                    if not meta_template:
                        meta_template = {k: v for k, v in wdoc.items() if k != "candidates"}
                    _persist_merged()
                    done_count = len(completed_by_id)
                    ts = datetime.now().strftime("%H:%M:%S")
                    print(
                        f"[{ts}] Completato {done_count}/{len(ids)}: {cid} ({elapsed_min:.1f} min) | "
                        f"{_summarize_completed_row(row)}",
                        flush=True,
                    )
                wjson.unlink(missing_ok=True)
                return row
            except Exception as exc:
                last_exc = exc
                if attempt == 0 and not cancel_event.is_set():
                    ts = datetime.now().strftime("%H:%M:%S")
                    print(f"[{ts}] Retry worker {cid} dopo errore transitorio...", flush=True)
                    if cancel_event.wait(10.0):
                        break
        with merge_lock:
            running_started.pop(cid, None)
        raise last_exc or RuntimeError(f"Worker {cid} annullato")

    def _heartbeat_loop():
        while not stop_heartbeat.wait(HEARTBEAT_SEC):
            with merge_lock:
                if not running_started:
                    continue
                now = time.monotonic()
                active_desc = ", ".join(
                    f"{cid} ({(now - started) / 60.0:.0f}m)"
                    for cid, started in running_started.items()
                )
                ts = datetime.now().strftime("%H:%M:%S")
                print(
                    f"[{ts}] Attivi ({len(completed_by_id)}/{len(ids)} completati): {active_desc}",
                    flush=True,
                )

    # On a fresh machine without indicator cache yet, warm up the cache with 1 candidate first.
    if not completed_by_id and len(remaining) > 1 and not _has_populated_indicator_cache():
        first_cid = remaining.pop(0)
        print(f"Warmup cache indicatori su primo candidato: {first_cid}...", flush=True)
        _run_one_candidate(first_cid, stagger_sec=0.0)

    hb_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
    hb_thread.start()
    pool = ThreadPoolExecutor(max_workers=eff_workers)
    try:
        futures = {}
        for idx, cid in enumerate(remaining):
            stagger = WORKER_STAGGER_SEC * idx if idx < eff_workers else 0.0
            futures[pool.submit(_run_one_candidate, cid, stagger)] = cid
        for fut in as_completed(futures):
            fut.result()
    except BaseException:
        cancel_event.set()
        _terminate_procs(active_procs, procs_lock)
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    else:
        pool.shutdown(wait=True)
    finally:
        stop_heartbeat.set()

    result = read_json(output)
    if not compatible(result) or {r["candidate"]["id"] for r in result.get("candidates", [])} != set(ids):
        raise RuntimeError(f"Output incompleto o incompatibile: {output}")
    return result["candidates"]


def stress(preset, candidate, name, ram_profile="auto"):
    schedule = schedule_path(preset)
    command = [sys.executable, "-u", str(ENGINE), *common_args(ram_profile), "--pairlist-schedule", str(schedule),
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
                and str(meta.get("schedule_meta", {}).get("path") or "").replace("\\", "/")
                == str(schedule).replace("\\", "/"))

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


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline resumable prop benchmark orchestrator.")
    parser.add_argument(
        "--workers",
        type=int,
        default=0,
        help="Parallel worker count (0 = auto from physical RAM & CPU cores).",
    )
    parsed = parser.parse_args(argv if argv is not None else [])
    workers = parsed.workers if parsed.workers > 0 else choose_workers()
    os.chdir(ROOT)
    OUT.mkdir(parents=True, exist_ok=True)
    ram_gb = detect_total_ram_gb()
    cpus = os.cpu_count() or 1
    print(
        f"Hardware: {ram_gb:.1f} GB RAM, {cpus} core CPU -> {workers} worker "
        f"({'paralleli, ram-profile=normal' if workers > 1 else 'singolo, ram-profile=auto'}).",
        flush=True,
    )
    all_ids = [c["id"] for c in candidates()]
    prepare_data()
    rows = grid("base", all_ids, 14, "grid_base_24cands.json", workers=workers)
    selected = top_four(rows)
    print("Top 4: " + ", ".join(selected), flush=True)
    options = []
    for preset in ("base", "strict", "bluechip"):
        rows = grid(preset, selected, 7, f"grid_top4_{preset}_7d_crash.json", workers=workers)
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
    second = winners["two_step"]
    if (second["preset"], second["candidate"]) != (first["preset"], first["candidate"]) and workers >= 2:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(stress, first["preset"], first["candidate"], "suite_winner_stress.json", "normal")
            f2 = pool.submit(stress, second["preset"], second["candidate"], "suite_winner_2step_stress.json", "normal")
            f1.result()
            f2.result()
    else:
        stress(first["preset"], first["candidate"], "suite_winner_stress.json")
        if (second["preset"], second["candidate"]) != (first["preset"], first["candidate"]):
            stress(second["preset"], second["candidate"], "suite_winner_2step_stress.json")
    run_script("prop_shock_bounds.py")
    print("=== TUTTO COMPLETATO! ORA INCOLLA IL PROMPT 2 IN CODEX ===", flush=True)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (RuntimeError, subprocess.CalledProcessError, KeyboardInterrupt) as exc:
        print(f"\nInterrotto: {exc}\nRilanciare lo stesso comando per riprendere.", file=sys.stderr)
        sys.exit(1)
