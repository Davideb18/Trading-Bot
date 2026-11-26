"""NFIX8-Natural ('unlimited' stake) parallel research benchmark orchestrator.

Evaluates 100% native NostalgiaForInfinityX8_Prop sizing (`stake_amount = "unlimited"`)
across all 3 objective dynamic hourly (`1h`) universes (`bluechip`, `strict`, `base`):
  - Closed-bar rolling 24h volume + age/volatility/ROC/market-cap filters
    updated every hour (including archived delisted crash coins, zero hindsight blacklist)
  - 1-minute (`1m`) intrabar wick fidelity + strict HyroTrader rules (00:00 UTC rollover,
    40% consistency pause at $175, qualifying days >= $250 & >= 1%, 25% Funded margin cap)
  - 102 weekly rolling cohorts over 2 years (2024-11-15 -> 2026-11-15)
  - 31 daily cohorts across the Oct 2025 systemic flash crash (2025-09-25 -> 2025-10-25)
  - 57 daily cohorts across the Sep-Nov 2026 volatility window (2026-09-15 -> 2026-11-10)
  - 5-scenario execution slippage stress suite (up to 28 bps + strict wick) on winners

Resumable at individual candidate granularity; safe to stop (`Ctrl+C`) and restart anytime.
Research mode permits unverified historical evidence, not a certification of live parity.
Hybrid market caps and synthetic mark/funding require explicit command-line options.
"""

from __future__ import annotations

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
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

ROOT = Path(__file__).resolve().parents[1]
OUT = Path("user_data/forensic/strict_validation")
ENGINE = Path("scripts/prop_backtest_1to1.py")
STRATEGY = Path("user_data/strategies/NostalgiaForInfinityX8_Prop.py")
TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1d")
DATA_START = "2024-07-01"  # More than 120 closed daily candles before the first cohort.
PRESETS_ORDER = ("bluechip", "strict", "base")
SCENARIOS = {
    "base_0bps",
    "friction_10bps_rt",
    "stress_28bps_rt",
    "strict_wick_guard",
    "stress_28bps_strict_wick",
}
DETAIL_CACHE_DIR = Path("user_data/forensic/detail_mmap_cache")
DETAIL_CACHE_MIN_FREE_BYTES = 25 * 1024**3
COMMON = [
    "--start",
    "2024-11-15",
    "--end",
    "2026-11-15",
    "--timeframe-detail",
    "1m",
    "--ram-profile",
    "auto",
]
HEARTBEAT_SEC = 300.0
WORKER_STAGGER_SEC = 4.0
RESEARCH_MODE = False
LOCAL_DATA_ONLY = False
ALLOW_SYNTHETIC = False
MCAP_MODE = "strict"


def research_metadata():
    return {
        "mode": "research" if RESEARCH_MODE else "strict",
        "local_data_only": LOCAL_DATA_ONLY,
        "mcap_mode": MCAP_MODE,
        "allow_synthetic_market_data": ALLOW_SYNTHETIC,
        "live_parity_certified": False,
        "limitations": [
            "Hourly whitelist and minute candles do not reproduce tick execution or historical spreads.",
            "Historical instrument classification and market-cap observation vintages are unverified.",
        ] + (["Local data universe only: missing instruments can bias historical volume rankings."] if LOCAL_DATA_ONLY else []) + (["Hybrid market caps use current snapshots or stale observations: lookahead is possible."]
             if MCAP_MODE == "hybrid" else [])
          + (["Missing mark/funding may be synthesized; costs and drawdown are approximations."]
             if ALLOW_SYNTHETIC else []),
    }


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


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    _replace_atomic(temp, path)


def natural_candidates():
    """Read NATURAL_NFIX8_GRID via AST without importing Freqtrade in the parent process."""
    tree = ast.parse(ENGINE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "NATURAL_NFIX8_GRID":
            grid = ast.literal_eval(node.value)
            if len(grid) != 8 or len({c["id"] for c in grid}) != 8:
                raise RuntimeError("Expected exactly 8 distinct NATURAL_NFIX8_GRID candidates.")
            for c in grid:
                if c.get("stake_amount") != "unlimited":
                    raise RuntimeError(f"Non-natural stake_amount in NATURAL_NFIX8_GRID: {c}")
            return grid
    raise RuntimeError("NATURAL_NFIX8_GRID missing in prop_backtest_1to1.py.")


def hourly_schedule_path(preset):
    return OUT / f"pairlist_schedule_{preset}_1h_2y.json"


def present(path):
    return Path(path).is_file() and Path(path).stat().st_size > 0


def detect_total_ram_gb():
    try:
        if sys.platform == "win32":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                    (name, ctypes.c_ulonglong)
                    for name in (
                        "total_phys",
                        "avail_phys",
                        "total_page",
                        "avail_page",
                        "total_virtual",
                        "avail_virtual",
                        "avail_extended",
                    )
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.total_phys / (1024.0**3)
        if sys.platform == "darwin":
            return int(subprocess.check_output(["sysctl", "-n", "hw.memsize"])) / (1024.0**3)
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return (pages * page_size) / (1024.0**3)
    except Exception:
        pass
    return 8.0


def choose_workers(ram_gb=None, cpu_count=None):
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
    cache_dir = DETAIL_CACHE_DIR if DETAIL_CACHE_DIR.is_absolute() else ROOT / DETAIL_CACHE_DIR
    try:
        free_bytes = shutil.disk_usage(ROOT).free
    except OSError:
        return False
    if free_bytes >= DETAIL_CACHE_MIN_FREE_BYTES:
        return True
    return cache_dir.is_dir() and free_bytes >= (8 * 1024**3) and any(cache_dir.rglob("*.complete"))


def common_args(ram_profile="auto"):
    args = list(COMMON)
    if RESEARCH_MODE:
        args.append("--allow-research-schedule")
    if RESEARCH_MODE and ALLOW_SYNTHETIC:
        args.append("--allow-synthetic-market-data")
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


def _archive_issues_and_pairs():
    issues = []
    archive = OUT / "archive"
    manifest = read_json(OUT / "archive_download_manifest.json")
    if not isinstance(manifest, dict) or manifest.get("sample_only", True):
        issues.append("archive_download_manifest.json mancante o sample_only")
    elif manifest.get("start", "9999") > ("2024-11-01" if LOCAL_DATA_ONLY else DATA_START) or manifest.get("end", "") < "2026-11-18":
        issues.append(
            f"archive_download_manifest.json intervallo incompleto ({manifest.get('start')}..{manifest.get('end')})"
        )
    records = manifest.get("results", []) if isinstance(manifest, dict) else []
    if not records:
        issues.append("archive_download_manifest.json senza risultati")
    archived_pairs = set()
    missing_archive = []
    for record in records:
        filename = str(record.get("output", "")).replace("\\", "/").rsplit("/", 1)[-1]
        if not filename or not present(archive / record["symbol"] / filename):
            missing_archive.append(f"{record.get('symbol')}/{filename}")
            continue
        archived_pairs.add(record["symbol"][:-4] + "/USDT:USDT")
    if missing_archive:
        issues.append(f"file archive delisted mancanti ({len(missing_archive)}, es. {missing_archive[0]})")
    return issues, archived_pairs


def _futures_files_issues(pairs, archived_pairs):
    issues = []
    futures = Path("user_data/data/bybit/futures")
    missing_ohlcv = []
    missing_mark_funding = []
    for pair in sorted(pairs):
        prefix = pair.replace("/", "_").replace(":", "_")
        for tf in TIMEFRAMES:
            if not present(futures / f"{prefix}-{tf}-futures.feather"):
                missing_ohlcv.append(f"{prefix}-{tf}")
        for kind in ("mark", "funding_rate"):
            if not present(futures / f"{prefix}-1h-{kind}.feather"):
                missing_mark_funding.append(f"{prefix}-{kind}")
    if missing_ohlcv:
        issues.append(f"candele futures mancanti ({len(missing_ohlcv)}, es. {', '.join(missing_ohlcv[:5])})")
    if missing_mark_funding and not (RESEARCH_MODE and ALLOW_SYNTHETIC):
        issues.append(
            f"mark/funding_rate mancanti ({len(missing_mark_funding)}, es. {', '.join(missing_mark_funding[:5])})"
        )
    return issues


def hourly_schedule_issues():
    """Validate all 3 hourly (1h) dynamic schedules and all referenced futures/archive files."""
    issues = []
    pairs = set()
    for preset in PRESETS_ORDER:
        spath = hourly_schedule_path(preset)
        doc = read_json(spath)
        if not isinstance(doc, dict) or not doc:
            issues.append(f"schedule oraria mancante o illeggibile: {spath}")
            continue
        if doc.get("resolution") != "1h":
            issues.append(f"schedule {preset}: resolution != '1h'")
            continue
        if doc.get("mcap_mode") != MCAP_MODE or (not RESEARCH_MODE and doc.get("zero_lookahead") is not True):
            issues.append(f"schedule {preset}: historical evidence unverified or market-cap proxies enabled")
            continue
        if doc.get("pairlist_order") != "volume_top_n_then_filters":
            issues.append(f"schedule {preset}: rigenerare con ordine filtri corretto")
            continue
        if doc.get("coverage_diagnostics", {}).get("empty_hours") != 0:
            issues.append(f"schedule {preset}: empty_hours != 0")
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

    arch_issues, archived_pairs = _archive_issues_and_pairs()
    issues.extend(arch_issues)
    pairs.update(archived_pairs)
    if not pairs:
        issues.append("nessuna coppia trovata nelle schedule orarie")
    else:
        issues.extend(_futures_files_issues(pairs, archived_pairs))
    return issues


def raw_market_data_present():
    """Return True when Marco's raw Bybit futures + archives + CoinMetrics/CoinGecko caches already exist."""
    arch_issues, archived_pairs = _archive_issues_and_pairs()
    if arch_issues:
        return False
    futures = Path("user_data/data/bybit/futures")
    if not futures.is_dir():
        return False
    # Existing files may start too late even when all required timeframes exist.
    try:
        import pandas as pd

        anchor = pd.read_feather(futures / "BTC_USDT_USDT-1d-futures.feather", columns=["date"])
        dates = pd.to_datetime(anchor["date"], utc=True).drop_duplicates()
        if (dates < pd.Timestamp("2024-11-15", tz="UTC")).sum() < 120:
            return False
    except (OSError, ValueError, KeyError):
        return False
    # Check BTC + archived pairs at minimum; hourly schedule check validates the full universe next.
    probe_pairs = set(archived_pairs) | {"BTC/USDT:USDT"}
    legacy_base = read_json(OUT / "pairlist_schedule_base_2y.json")
    if isinstance(legacy_base, dict):
        probe_pairs.update(legacy_base.get("ever_whitelisted_2y", []))
    return not _futures_files_issues(probe_pairs, archived_pairs)


def inspect_candle_file(path, timeframe, start, end, *, funding=False):
    """Check real file contents, not merely presence. Funding is an event series."""
    import numpy as np
    import pandas as pd

    frame = pd.read_feather(path)
    if frame.empty:
        raise ValueError("file vuoto")
    dates = pd.to_datetime(frame["date"], utc=True, errors="raise")
    if dates.isna().any() or not dates.is_monotonic_increasing or dates.duplicated().any():
        raise ValueError("timestamp mancanti, duplicati o fuori ordine")
    mark = Path(path).name.endswith("-mark.feather")
    columns = ["open"] if funding else ["open", "high", "low", "close"] + ([] if mark else ["volume"])
    values = frame[columns].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError("valori OHLCV/funding non finiti")
    if not funding and (
        (values[:, :4] <= 0).any() or (not mark and (values[:, 4] < 0).any())
        or (values[:, 1] < values[:, [0, 2, 3]].max(axis=1)).any()
        or (values[:, 2] > values[:, [0, 1, 3]].min(axis=1)).any()
    ):
        raise ValueError("prezzi/volumi OHLCV invalidi")
    seconds = dates.dt.as_unit("s").to_numpy(dtype=np.int64)
    stride = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}[timeframe]
    if funding:
        # Funding intervals can change per instrument; do not fabricate hourly events.
        if seconds[0] > start + 86400 or seconds[-1] < end - 86400:
            raise ValueError("funding non copre il periodo richiesto (tolleranza 24h)")
        funding_events = seconds[(seconds >= start - 86400) & (seconds <= end + 86400)]
        if len(funding_events) > 1 and (np.diff(funding_events) > 86400).any():
            raise ValueError("intervallo funding superiore a 24h; verificare archivio")
    else:
        if (seconds % stride != 0).any():
            raise ValueError("timestamp non allineati al timeframe")
        first, last = start // stride * stride, (end - 1) // stride * stride
        lo, hi = np.searchsorted(seconds, [first, last], side="left")
        if hi >= len(seconds) or seconds[lo] != first or seconds[hi] != last:
            raise ValueError("copertura temporale insufficiente per la whitelist")
        if (np.diff(seconds[lo:hi + 1]) != stride).any():
            raise ValueError("buchi nelle candele durante il periodo richiesto")
    return {"path": str(path), "rows": len(frame), "first": dates.iloc[0].isoformat(), "last": dates.iloc[-1].isoformat()}


def run_preflight():
    """Offline gate: validate schedules, warmup, prices and explicit approximations."""
    start = int(datetime(2024, 11, 15, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2026, 11, 15, tzinfo=timezone.utc).timestamp())
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "research": research_metadata(),
              "issues": [], "warnings": [], "files": [], "schedule_evidence": {}}
    issues = report["issues"]
    try:
        issues.extend(hourly_schedule_issues())
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        issues.append(f"Schedule/manifest non valido: {exc}")
    required = {"BTC/USDT:USDT": [start, end]}
    for preset in PRESETS_ORDER:
        doc = read_json(hourly_schedule_path(preset))
        report["schedule_evidence"][preset] = {k: doc.get(k) for k in (
            "zero_lookahead", "validation_status", "mcap_mode", "limitations", "coverage_diagnostics",
            "mcap_provenance_counts", "archived_delisted_without_mcap")}
        try:
            schedule = {int(k): v for k, v in doc["schedule"].items()}
            if any(ts % 3600 or not isinstance(pairs, list) or any(not isinstance(p, str) or "/USDT:USDT" not in p for p in pairs)
                   for ts, pairs in schedule.items()):
                raise ValueError("bucket non orario o coppie non valide")
            if any(day not in schedule for day in range(start, end, 86400)):
                raise ValueError("ancore giornaliere mancanti")
            active = []
            schedule_pairs = set()
            for ts in range(start, end, 3600):
                active = schedule.get(ts, active)
                if not active:
                    raise ValueError(f"whitelist vuota a {ts}")
                for pair in active:
                    schedule_pairs.add(pair)
                    bounds = required.setdefault(pair, [ts, ts + 3600])
                    bounds[0], bounds[1] = min(bounds[0], ts), max(bounds[1], ts + 3600)
            if not (schedule_pairs | {"BTC/USDT:USDT"}).issubset(set(doc.get("ever_whitelisted_2y", []))):
                raise ValueError("universo incompleto")
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            issues.append(f"{preset}: {exc}")
    if not issues:
        import pandas as pd

        futures = ROOT / "user_data/data/bybit/futures"
        for index, (pair, (first, last)) in enumerate(sorted(required.items()), 1):
            print(f"Preflight {index}/{len(required)}: {pair}", flush=True)
            prefix = pair.replace("/", "_").replace(":", "_")
            for tf in TIMEFRAMES:
                path = futures / f"{prefix}-{tf}-futures.feather"
                try:
                    report["files"].append(inspect_candle_file(path, tf, first, last))
                    if pair == "BTC/USDT:USDT" and tf == "1d":
                        dates = pd.to_datetime(pd.read_feather(path, columns=["date"])["date"], utc=True)
                        warmup = dates[(dates >= pd.Timestamp("2024-07-18", tz="UTC")) & (dates < pd.Timestamp("2024-11-15", tz="UTC"))]
                        if len(warmup) < 120:
                            issues.append("BTC: meno di 120 candele giornaliere di warmup")
                except Exception as exc:
                    issues.append(f"{path.name}: {exc}")
            for kind in ("mark", "funding_rate"):
                path = futures / f"{prefix}-1h-{kind}.feather"
                if not present(path) and RESEARCH_MODE and ALLOW_SYNTHETIC:
                    report["warnings"].append(f"{pair}: {kind} mancante, fallback sintetico autorizzato")
                    continue
                try:
                    report["files"].append(inspect_candle_file(path, "1h", first, last, funding=kind == "funding_rate"))
                except Exception as exc:
                    issues.append(f"{path.name}: {exc}")
    report["archive_minutes_repair"] = read_json(OUT / "archive_minutes_repair.json")
    report["passed"] = not issues
    atomic_json(OUT / "natural_preflight.json", report)
    for issue in issues[:20]:
        print("BLOCCO: " + issue, flush=True)
    return report


def run(command):
    print("\n>>> " + subprocess.list2cmdline(command), flush=True)
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def run_script(name, *args):
    run([sys.executable, "-u", "scripts/" + name, *map(str, args)])


def prepare_data_and_hourly_schedules():
    if not hourly_schedule_issues() and raw_market_data_present():
        print("Dati Bybit, archivi delisted e 3 schedule dinamiche orarie (1h) pronti.", flush=True)
        return

    if not LOCAL_DATA_ONLY and not raw_market_data_present():
        print("Dati raw incompleti: avvio download completo Bybit + archivi storici...", flush=True)
        pairs = str(OUT / "eligible_bybit_pairs.json")
        run_script("prop_prepare_benchmark_data.py", "--anti-survivorship", "--export-volume-universe", "--export-download-pairs", pairs)
        override_path = OUT / "_download_override.json"
        atomic_json(
            override_path,
            {"dry_run": True, "exchange": {"demo_trading": False, "key": "", "secret": ""}},
        )
        try:
            run(
                [
                    sys.executable,
                    "-u",
                    "scripts/prop_download_data.py",
                    "--config",
                    "user_data/config_prop_nfix8.json",
                    "--config",
                    str(override_path),
                    "--pairs-file",
                    pairs,
                    "--trading-mode",
                    "futures",
                    "--timeframes",
                    *TIMEFRAMES,
                    "--timerange",
                    DATA_START.replace("-", "") + "-20261118",
                    "--prepend",
                ]
            )
        finally:
            override_path.unlink(missing_ok=True)
        run_script("prop_archive_data.py", "--start", DATA_START, "--end", "2026-11-18", "--delete-raw")
        run_script("prop_marketcap_archive.py")
    else:
        print("Dati Bybit e archivi gia presenti su disco: nessun nuovo download necessario.", flush=True)

    print(f"Costruzione whitelist orarie (1h, market-cap={MCAP_MODE}, evidenza storica non certificata)...", flush=True)
    sched_args = [
        "--start",
        "2024-11-15",
        "--end",
        "2026-11-15",
        "--resolution",
        "1h",
        "--anti-survivorship",
        "--include-archived-delisted",
        "--mcap-mode",
        MCAP_MODE,
        "--build-presets-dir",
        str(OUT),
    ]
    if LOCAL_DATA_ONLY and not present(OUT / "instruments.json"):
        raise RuntimeError("Modalita locale: manca instruments.json; nessun download avviato.")
    if not LOCAL_DATA_ONLY and MCAP_MODE == "hybrid" and not present(OUT / "coingecko_mcap_snapshot.json"):
        sched_args.append("--fetch-coingecko")
    run_script("prop_prepare_benchmark_data.py", *sched_args)

    issues = hourly_schedule_issues()
    if issues:
        detail = "; ".join(issues[:6])
        raise RuntimeError(
            f"Dati o schedule orarie incompleti: nessun backtest avviato ({detail})."
        )


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def complete_row(row, *, require_train_oos=True):
    try:
        windows = (
            ("train_1y_funded", "oos_1y_funded", "full_2y_continuous_funded")
            if require_train_oos
            else ("full_2y_continuous_funded",)
        )
        for window in windows:
            for suffix in ("", "_two_step"):
                for key in ("net_pnl", "max_overall_dd_wick", "funded_margin_gt_25pct_bars"):
                    if not math.isfinite(float(row[window + suffix][key])):
                        return False
        for group, expected_cohorts in (
            ("program_comparison", None),
            ("oct_2025_daily_crash_sweep", 31),
            ("autumn_2026_daily_crash_sweep", 57),
        ):
            grp = row.get(group)
            if not isinstance(grp, dict):
                return False
            for program in ("one_step", "two_step"):
                summary = grp[program + "_summary"]
                if summary["total_cohorts"] <= 0 or "fail_official" not in summary:
                    return False
                if expected_cohorts is not None and summary["total_cohorts"] != expected_cohorts:
                    return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


def safe(row):
    if not complete_row(row):
        return False
    for item in walk(row):
        for key, value in item.items():
            if (
                key
                in (
                    "fail_official",
                    "funded_margin_gt_25pct_bars",
                    "funded_notional_gt_2x_bars",
                    "official_daily_breach_wick_bars",
                    "official_overall_breach_wick_bars",
                    "total_official_daily_breach_wick_bars",
                    "total_official_overall_breach_wick_bars",
                )
                and value != 0
            ):
                return False
            if key in ("official_breach_close", "official_breach_wick") and value is not None:
                return False
            if isinstance(value, str) and value == "FAIL_OFFICIAL":
                return False
    return True


def score(row, program=None):
    """Rank safe candidates by OOS/2Y return-to-risk, qualification rate, and payout rate."""
    programs = [program] if program else ["one_step", "two_step"]
    ret_ratios, qual_rates, payout_rates, net_pnls = [], [], [], []
    for name in programs:
        suffix = "_two_step" if name == "two_step" else ""
        for window in ("oos_1y_funded", "full_2y_continuous_funded"):
            stats = row[window + suffix]
            net_pnls.append(float(stats["net_pnl"]))
            # Floor denominator at $15 so sub-$15 wick noise does not outweigh real profit.
            ret_ratios.append(float(stats["net_pnl"]) / max(15.0, float(stats["max_overall_dd_wick"])))
        summary = row["program_comparison"][name + "_summary"]
        qual_rates.append(float(summary["qualify_rate_pct"]))
        payout_rates.append(float(summary["first_payout_rate_pct"]))
    return (
        round(sum(qual_rates) / len(qual_rates), 3),
        round(sum(payout_rates) / len(payout_rates), 3),
        round(sum(net_pnls) / len(net_pnls), 2),
        round(sum(ret_ratios) / len(ret_ratios), 4),
    )


def engine_signature(schedule, *, kind, step, slippage=10, strict_wick=True, candidate=None):
    payload = {
        "research": research_metadata(),
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
    # Strategy, configuration and imported risk/execution code also affect results.
    sources = {ROOT / STRATEGY, ROOT / "user_data/config_prop_nfix8.json", Path(__file__)}
    sources.update((ROOT / "user_data/strategies").glob("*.py"))
    sources.update((ROOT / "freqtrade").rglob("*.py"))
    for path in sorted(sources):
        digest.update(str(path).encode())
        digest.update(path.read_bytes() if path.is_file() else b"missing")
    for path in sorted((ROOT / "user_data/data/bybit/futures").glob("*.feather")):
        stat = path.stat()
        digest.update(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}".encode())
    return digest.hexdigest()


def _backup_incompatible(output):
    backup = output.with_suffix(output.suffix + ".previous")
    index = 1
    while backup.exists():
        backup = output.with_suffix(output.suffix + f".previous.{index}")
        index += 1
    _replace_atomic(output, backup)


def _has_indicator_cache_for_schedule(schedule_path, cache_dir=None):
    """Return True only when .backtest_cache already covers 100% of this schedule's 2Y universe."""
    cache_root = ROOT / ".backtest_cache"
    strat_file = ROOT / STRATEGY
    if not cache_root.is_dir() or not strat_file.is_file():
        return False
    sdoc = read_json(schedule_path)
    needed = set(sdoc.get("ever_whitelisted_2y") or [])
    if not needed:
        return False
    strat_mtime = int(strat_file.stat().st_mtime)
    prefix = "2024-11-15_2026-11-15_"
    suffix = f"_{strat_mtime}"
    for sub in ([cache_dir] if cache_dir is not None else cache_root.iterdir()):
        if not sub.is_dir() or (cache_dir is None and (not sub.name.startswith(prefix) or not sub.name.endswith(suffix))):
            continue
        mpath = sub / "_manifest.json"
        ppath = sub / "_price_prec.pkl"
        if not (present(mpath) and present(ppath)):
            continue
        mdoc = read_json(mpath)
        if mdoc.get("complete") and needed <= set(mdoc.get("universe") or []):
            return True
    return False


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
        raise RuntimeError(f"Worker fallito (exit={rc}, log={log_path}):\n{tail}")


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


def _summarize_completed_row(row):
    try:
        pnl = float(row["full_2y_continuous_funded"]["net_pnl"])
        oos = float(row["oos_1y_funded"]["net_pnl"])
        dd = float(row["full_2y_continuous_funded"]["max_overall_dd_wick"])
        os_s = row["program_comparison"]["one_step_summary"]
        oct_s = row["oct_2025_daily_crash_sweep"]["one_step_summary"]
        aut_s = row["autumn_2026_daily_crash_sweep"]["one_step_summary"]
        fail = (
            int(os_s.get("fail_official", 0))
            + int(oct_s.get("fail_official", 0))
            + int(aut_s.get("fail_official", 0))
        )
        res_pct = os_s.get("resolved_pass_rate_pct")
        res_txt = f"{res_pct}%" if res_pct is not None else "n/a"
        return (
            f"2y Net {pnl:+.2f}$ (OOS {oos:+.2f}$) | DD {dd:.2f}$ | "
            f"1-Step Pass {os_s['qualify_rate_pct']}% (chiuse: {res_txt}) | "
            f"CrashFail(Oct25/Aut26)={oct_s['fail_official']}/{aut_s['fail_official']} (Tot={fail})"
        )
    except (KeyError, TypeError, ValueError):
        return "ok"


def run_natural_grid(preset, ids, step, workers=1):
    schedule = hourly_schedule_path(preset)
    output = OUT / f"grid_natural_{preset}_1h.json"
    workers = max(1, int(workers))

    def compatible(doc):
        return (
            isinstance(doc, dict)
            and doc.get("schema_version") == 4
            and doc.get("cohort_step_days") == step
            and doc.get("slippage_bps_roundtrip") == 10
            and doc.get("strict_wick_guard") is True
            and doc.get("train_window", [""])[0][:10] == "2024-11-15"
            and doc.get("oos_window", ["", ""])[-1][:10] == "2026-11-15"
            and str(doc.get("schedule_meta", {}).get("path") or "").replace("\\", "/")
            == str(schedule).replace("\\", "/")
            and all(complete_row(r) for r in doc.get("candidates", []))
        )

    marker = output.with_suffix(output.suffix + ".run.json")
    eng_sig = engine_signature(schedule, kind="natural_grid", step=step, slippage=10, strict_wick=True)
    indicator_cache = ROOT / ".backtest_cache" / f"natural-{eng_sig}"
    doc = read_json(output)
    old = read_json(marker)
    matches = compatible(doc) and old.get("engine_signature") == eng_sig
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
    if workers_dir.is_dir():
        for cid in ids:
            if cid in completed_by_id:
                continue
            wpath = workers_dir / f"{output.stem}.{cid}.json"
            if not present(wpath):
                continue
            wdoc = read_json(wpath)
            wmarker = wpath.with_suffix(wpath.suffix + ".run.json")
            if compatible(wdoc) and read_json(wmarker).get("engine_signature") == eng_sig:
                for row in wdoc.get("candidates", []):
                    if (row.get("candidate") or {}).get("id") == cid and complete_row(row):
                        completed_by_id[cid] = row
                        if not meta_template:
                            meta_template = {k: v for k, v in wdoc.items() if k != "candidates"}
                        break
            wpath.unlink(missing_ok=True)
            wmarker.unlink(missing_ok=True)

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
    ram_prof = "normal" if eff_workers > 1 else "auto"
    print(
        f"\n=== UNIVERSO [{preset.upper()} 1h] ({output.name}): "
        f"{len(completed_by_id)}/{len(ids)} completati, {len(remaining)} rimanenti "
        f"con {eff_workers} worker (ram-profile={ram_prof}) ===",
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
        atomic_json(wjson.with_suffix(wjson.suffix + ".run.json"), {"engine_signature": eng_sig})
        command = [
            sys.executable,
            "-u",
            str(ENGINE),
            *common_args(ram_prof),
            "--cache-dir",
            str(indicator_cache),
            "--pairlist-schedule",
            str(schedule),
            "--cohort-step-days",
            str(step),
            "--slippage-bps",
            "10",
            "--strict-wick-guard",
            "--candidates",
            cid,
            "--grid-json",
            str(wjson),
        ]
        t0 = time.monotonic()
        with merge_lock:
            running_started[cid] = t0
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] [{preset}] Avviato: {cid} (log: {wlog})", flush=True)
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
                        f"[{ts}] [{preset}] Completato {done_count}/{len(ids)}: {cid} ({elapsed_min:.1f} min) | "
                        f"{_summarize_completed_row(row)}",
                        flush=True,
                    )
                wjson.unlink(missing_ok=True)
                return row
            except Exception as exc:
                last_exc = exc
                if attempt == 0 and not cancel_event.is_set():
                    ts = datetime.now().strftime("%H:%M:%S")
                    print(f"[{ts}] [{preset}] Retry worker {cid} dopo errore transitorio...", flush=True)
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
                    f"[{ts}] [{preset}] Attivi ({len(completed_by_id)}/{len(ids)} completati): {active_desc}",
                    flush=True,
                )

    # Ensure 100% of this schedule's universe is in .backtest_cache before parallel fan-out.
    if len(remaining) > 1 and eff_workers > 1 and not _has_indicator_cache_for_schedule(schedule, indicator_cache):
        first_cid = remaining.pop(0)
        print(
            f"[{preset}] Allineamento cache indicatori sul primo candidato ({first_cid}) prima del lancio parallelo...",
            flush=True,
        )
        _run_one_candidate(first_cid, stagger_sec=0.0)
        eff_workers = min(workers, len(remaining))

    if remaining:
        hb_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
        hb_thread.start()
        pool = ThreadPoolExecutor(max_workers=max(1, eff_workers))
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


def run_stress_suite(preset, candidate, name, ram_profile="auto"):
    schedule = hourly_schedule_path(preset)
    output = OUT / name
    marker = output.with_suffix(output.suffix + ".run.json")
    eng_sig = engine_signature(
        schedule, kind="natural_stress", step=7, slippage=10, strict_wick=True, candidate=candidate
    )
    command = [
        sys.executable,
        "-u",
        str(ENGINE),
        *common_args(ram_profile),
        "--cache-dir",
        str(ROOT / ".backtest_cache" / f"natural-{eng_sig}"),
        "--pairlist-schedule",
        str(schedule),
        "--cohort-step-days",
        "7",
        "--slippage-bps",
        "10",
        "--strict-wick-guard",
        "--candidates",
        candidate,
        "--suite-json",
        str(output),
    ]

    def compatible(doc):
        if not isinstance(doc, dict):
            return False
        meta = doc.get("meta", {})
        return (
            meta.get("schema_version") == 3
            and meta.get("timeframe_detail") == "1m"
            and meta.get("cohort_step_days") == 7
            and meta.get("start", "")[:10] == "2024-11-15"
            and meta.get("end", "")[:10] == "2026-11-15"
            and (meta.get("applied_candidate") or {}).get("id") == candidate
            and str(meta.get("schedule_meta", {}).get("path") or "").replace("\\", "/")
            == str(schedule).replace("\\", "/")
        )

    def complete(doc):
        scenarios = doc.get("scenarios", {})
        if not SCENARIOS <= set(scenarios.keys()):
            return False
        for result in scenarios.values():
            row = {
                "full_2y_continuous_funded": result.get("continuous_funded", {}),
                "full_2y_continuous_funded_two_step": result.get("continuous_funded_two_step", {}),
                "program_comparison": result.get("program_comparison"),
                "oct_2025_daily_crash_sweep": result.get("oct_2025_daily_crash_sweep"),
                "autumn_2026_daily_crash_sweep": result.get("autumn_2026_daily_crash_sweep"),
            }
            if not complete_row(row, require_train_oos=False):
                return False
        return True

    doc = read_json(output)
    old = read_json(marker)
    matches = compatible(doc) and old.get("engine_signature") == eng_sig
    if present(output) and not matches:
        _backup_incompatible(output)
        doc = {}
    atomic_json(marker, {"engine_signature": eng_sig, "preset": preset, "candidate": candidate})
    if matches and complete(doc):
        print(f"Completo: {output.name}; saltato.", flush=True)
        return doc
    run(command)
    res = read_json(output)
    if not compatible(res) or not complete(res):
        raise RuntimeError(f"Stress suite incompleta o incompatibile: {output}")
    return res


def build_summary_and_report(all_entries, winners, stress_docs):
    ranked_one_step = sorted(
        all_entries,
        key=lambda item: (safe(item["row"]), score(item["row"], "one_step"), item["preset"], item["row"]["candidate"]["id"]),
        reverse=True,
    )
    leaderboard = []
    lines = [
        "====================================================================================================",
        "  REPORT DEFINITIVO NFIX8-NATURAL (UNLIMITED STAKE, WHITELIST DINAMICA 1H, WICK 1M, CRASH SWEEP)   ",
        "====================================================================================================",
        "",
        f"Vincitore One-Step : [{winners['one_step']['preset'].upper()}] {winners['one_step']['candidate']}",
        f"Vincitore Two-Step : [{winners['two_step']['preset'].upper()}] {winners['two_step']['candidate']}",
        "",
        "CLASSIFICA COMPLETA (24 COMBINAZIONI = 3 UNIVERSI DINAMICI 1H x 8 CANDIDATI NFIX8 NATURALI):",
        "----------------------------------------------------------------------------------------------------",
    ]
    metadata = research_metadata()
    lines[1] = "  REPORT NFIX8-NATURAL - " + metadata["mode"].upper() + " - NON CERTIFICATO LIVE"
    lines[4:4] = ["Limiti: " + item for item in metadata["limitations"]] + [""]
    for rank, item in enumerate(ranked_one_step, 1):
        preset = item["preset"]
        r = item["row"]
        c = r["candidate"]
        f2 = r["full_2y_continuous_funded"]
        oos = r["oos_1y_funded"]
        os_s = r["program_comparison"]["one_step_summary"]
        ts_s = r["program_comparison"]["two_step_summary"]
        oct_s = r["oct_2025_daily_crash_sweep"]["one_step_summary"]
        aut_s = r["autumn_2026_daily_crash_sweep"]["one_step_summary"]
        wd = ((f2.get("funded_withdrawal_policies") or {}).get("reset_5250_to_5000") or {})
        is_safe = safe(r)
        entry = {
            "rank_one_step": rank,
            "preset": preset,
            "candidate_id": c["id"],
            "max_open_trades": c["max_open_trades"],
            "max_slot_notional_usdt": c["max_slot_notional_usdt"],
            "max_aggregate_notional_usdt": c["max_aggregate_notional_usdt"],
            "macro": c["macro"],
            "safe_all_constraints": is_safe,
            "net_pnl_2y_usdt": f2["net_pnl"],
            "net_pnl_oos_1y_usdt": oos["net_pnl"],
            "max_daily_dd_wick_usdt": f2["max_daily_dd_wick"],
            "max_overall_dd_wick_usdt": f2["max_overall_dd_wick"],
            "one_step_qualify_rate_pct": os_s["qualify_rate_pct"],
            "one_step_resolved_pass_rate_pct": os_s.get("resolved_pass_rate_pct"),
            "one_step_median_days_to_qualify": os_s["median_days_to_qualify"],
            "one_step_median_days_to_first_payout": os_s["median_days_to_first_payout"],
            "two_step_qualify_rate_pct": ts_s["qualify_rate_pct"],
            "two_step_resolved_pass_rate_pct": ts_s.get("resolved_pass_rate_pct"),
            "oct_2025_crash_fail_official": oct_s["fail_official"],
            "oct_2025_crash_worst_daily_dd_wick": oct_s["worst_daily_dd_wick"],
            "autumn_2026_crash_fail_official": aut_s["fail_official"],
            "autumn_2026_crash_worst_daily_dd_wick": aut_s["worst_daily_dd_wick"],
            "trader_80pct_withdrawn_2y_usdt": wd.get("withdrawn_net_trader_80pct_usdt"),
            "payout_count_2y": wd.get("payout_count"),
        }
        leaderboard.append(entry)
        res_pct = os_s.get("resolved_pass_rate_pct")
        res_str = f"{res_pct:.1f}%" if isinstance(res_pct, (int, float)) else "n/a"
        lines.append(
            f"#{rank:02d} [{'SAFE' if is_safe else 'FAIL'}] {preset.upper():8s} | {c['id']:26s} | "
            f"2Y Net: {f2['net_pnl']:+8.2f}$ (OOS: {oos['net_pnl']:+7.2f}$) | "
            f"MaxWickDD: {f2['max_overall_dd_wick']:5.2f}$ | "
            f"1-Step: {os_s['qualify_rate_pct']:5.1f}% (chiuse {res_str}, med {os_s['median_days_to_qualify']}d) | "
            f"Oct25 DD {oct_s['worst_daily_dd_wick']:.1f}$ (Fail {oct_s['fail_official']}) | "
            f"Aut26 DD {aut_s['worst_daily_dd_wick']:.1f}$ (Fail {aut_s['fail_official']})"
        )

    lines.append("")
    lines.append("STRESS TEST 28 BPS SLIPPAGE + STRICT WICK SUI VINCITORI:")
    lines.append("----------------------------------------------------------------------------------------------------")
    for label, sdoc in stress_docs.items():
        sc28 = (sdoc.get("scenarios") or {}).get("stress_28bps_strict_wick") or {}
        cf = sc28.get("continuous_funded") or {}
        os_s = ((sc28.get("program_comparison") or {}).get("one_step_summary") or {})
        oct_s = ((sc28.get("oct_2025_daily_crash_sweep") or {}).get("one_step_summary") or {})
        aut_s = ((sc28.get("autumn_2026_daily_crash_sweep") or {}).get("one_step_summary") or {})
        lines.append(
            f"  [{label}] 28bps+StrictWick -> 2Y Net: {cf.get('net_pnl')}$ | "
            f"MaxWickDD: {cf.get('max_overall_dd_wick')}$ | "
            f"1-Step Qual: {os_s.get('qualify_rate_pct')}% | "
            f"Fail(2Y/Oct25/Aut26)={os_s.get('fail_official')}/{oct_s.get('fail_official')}/{aut_s.get('fail_official')}"
        )

    report_text = "\n".join(lines) + "\n"
    summary_doc = {
        "research": metadata,
        "preflight": read_json(OUT / "natural_preflight.json"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "winners": winners,
        "leaderboard": leaderboard,
    }
    atomic_json(OUT / "natural_benchmark_summary.json", summary_doc)
    atomic_text(OUT / "natural_benchmark_report.txt", report_text)
    return summary_doc, report_text


def main(argv=None):
    global RESEARCH_MODE, ALLOW_SYNTHETIC, MCAP_MODE, LOCAL_DATA_ONLY
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workers",
        type=int,
        default=0,
        help="Parallel worker count (0 = auto from physical RAM & CPU cores).",
    )
    parser.add_argument("--research", action="store_true", help="Allow unverified schedules; label results as research.")
    parser.add_argument("--local-data-only", action="store_true", help="Research only: reuse local universe without downloading history.")
    parser.add_argument("--mcap-mode", choices=("strict", "hybrid"), default="strict")
    parser.add_argument("--allow-synthetic-market-data", action="store_true", help="Research only: permit missing mark/funding approximations.")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check-only", action="store_true", help="Offline data checks only; no download and no backtest.")
    action.add_argument("--prepare-only", action="store_true", help="Download/prepare data and check it; no backtest.")
    parsed = parser.parse_args(argv)
    if not parsed.research and (parsed.local_data_only or parsed.allow_synthetic_market_data or parsed.mcap_mode != "strict"):
        parser.error("--mcap-mode hybrid and --allow-synthetic-market-data require --research")
    if parsed.workers < 0:
        parser.error("--workers must be >= 0")
    RESEARCH_MODE, ALLOW_SYNTHETIC, MCAP_MODE = parsed.research, parsed.allow_synthetic_market_data, parsed.mcap_mode
    LOCAL_DATA_ONLY = parsed.local_data_only
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
    print(json.dumps(research_metadata(), indent=2), flush=True)
    if not parsed.check_only:
        try:
            prepare_data_and_hourly_schedules()
        except Exception as exc:
            atomic_json(OUT / "natural_preflight.json", {
                "passed": False, "research": research_metadata(),
                "issues": [f"Preparazione fallita: {exc}"], "warnings": [],
            })
            raise
    preflight = run_preflight()
    if preflight["issues"]:
        raise SystemExit("Controlli falliti; nessun backtest avviato. Vedi " + str(OUT / "natural_preflight.json"))
    if parsed.check_only or parsed.prepare_only:
        print("Controlli completati. Nessun backtest avviato.", flush=True)
        return
    cands = natural_candidates()
    all_ids = [c["id"] for c in cands]

    all_entries = []
    safe_options = []
    for preset in PRESETS_ORDER:
        rows = run_natural_grid(preset, all_ids, step=7, workers=workers)
        for row in rows:
            all_entries.append({"preset": preset, "row": row})
            if safe(row):
                safe_options.append((preset, row))

    if not safe_options:
        raise RuntimeError("Nessun candidato naturale rispetta tutti i vincoli ufficiali di sicurezza.")

    winners = {}
    for program in ("one_step", "two_step"):
        preset, row = max(
            safe_options,
            key=lambda item: (score(item[1], program), item[0], item[1]["candidate"]["id"]),
        )
        winners[program] = {
            "preset": preset,
            "candidate": row["candidate"]["id"],
            "score": score(row, program),
        }

    first = winners["one_step"]
    second = winners["two_step"]
    stress_docs = {}
    if (second["preset"], second["candidate"]) != (first["preset"], first["candidate"]):
        if workers >= 2:
            with ThreadPoolExecutor(max_workers=2) as pool:
                f1 = pool.submit(
                    run_stress_suite, first["preset"], first["candidate"], "suite_natural_winner_stress.json", "normal"
                )
                f2 = pool.submit(
                    run_stress_suite,
                    second["preset"],
                    second["candidate"],
                    "suite_natural_winner_2step_stress.json",
                    "normal",
                )
                stress_docs["one_step_winner"] = f1.result()
                stress_docs["two_step_winner"] = f2.result()
        else:
            stress_docs["one_step_winner"] = run_stress_suite(
                first["preset"], first["candidate"], "suite_natural_winner_stress.json"
            )
            stress_docs["two_step_winner"] = run_stress_suite(
                second["preset"], second["candidate"], "suite_natural_winner_2step_stress.json"
            )
    else:
        stress_docs["one_step_and_two_step_winner"] = run_stress_suite(
            first["preset"], first["candidate"], "suite_natural_winner_stress.json"
        )

    run_script("prop_shock_bounds.py")
    _, report_text = build_summary_and_report(all_entries, winners, stress_docs)
    print("\n" + report_text, flush=True)
    print(
        f"COMPLETATO! Invia a Davide:\n"
        f"  1) {OUT / 'natural_benchmark_report.txt'}\n"
        f"  2) {OUT / 'natural_benchmark_summary.json'}\n"
        f"  3) {OUT / 'suite_natural_winner_stress.json'}",
        flush=True,
    )


if __name__ == "__main__":
    main()
