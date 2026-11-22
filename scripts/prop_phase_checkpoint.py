"""Opt-in atomic checkpoints at independent offline simulation boundaries."""
from __future__ import annotations

import copy
import functools
import hashlib
import inspect
import json
import logging
from datetime import datetime
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def configure_checkpoints(bt, args, repository, datadir):
    destination = getattr(args, "phase_checkpoint_dir", "")
    if not destination:
        return
    sources = [repository / "scripts" / name for name in
               ("prop_backtest_1to1.py", "prop_callback_snapshot.py", "prop_phase_checkpoint.py",
                "prop_detail_array_cache.py")]
    sources += list((repository / "freqtrade").rglob("*.py"))
    sources += list((repository / "user_data" / "strategies").glob("*.py"))
    inputs = [Path(args.config)]
    if args.pairlist_schedule:
        inputs.append(Path(args.pairlist_schedule))
    fingerprint = {
        "sources": [(str(p.relative_to(repository)), hashlib.sha256(p.read_bytes()).hexdigest()) for p in sorted(sources)],
        "inputs": [hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs],
        "data": [(str(p.relative_to(datadir)), p.stat().st_size, p.stat().st_mtime_ns)
                 for p in sorted(Path(datadir).rglob("*.feather"))],
        "args": {k: v for k, v in vars(args).items() if k not in
                 ("output_json", "grid_json", "suite_json", "phase_checkpoint_dir", "overwrite_suite")},
    }
    bt._phase_checkpoint_root = Path(destination) / digest(fingerprint)
    bt._phase_checkpoint_root.mkdir(parents=True, exist_ok=True)


def phase_checkpoint(function):
    """Cache complete phase results; every subsequent real phase resets its ledger.

    Callers of run_cached_cohort only inspect its result and prop_pass_dt.
    Restore transition timestamps on cache hits; never restore a partial trade ledger.
    """
    signature = inspect.signature(function)

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        arguments = signature.bind(*args, **kwargs)
        arguments.apply_defaults()
        values = arguments.arguments
        bt = values.get("bt", values.get("self"))
        root = getattr(bt, "_phase_checkpoint_root", None)
        if root is None:
            return function(*args, **kwargs)
        continuous = function.__name__ == "_run_continuous_pass"
        profile = values["profile"] if continuous else bt._prop_profile
        context = {
            "function": function.__name__, "profile": profile,
            "candidate": getattr(bt, "_checkpoint_candidate", None),
            "policy": bt._prop_policy, "fee": bt.fee,
            "slippage": bt._extra_slippage_bps, "strict_wick": bt._strict_wick_guard,
            "arguments": {k: v for k, v in values.items() if k not in
                          ("bt", "self", "cfg", "profile", "ohclv_lists", "analyzed_dfs")},
        }
        if not continuous:
            context["flags"] = [bt._stop_on_halt, bt._stop_on_pass, bt._ignore_target_pause]
        path = root / (digest(context) + ".json")
        if path.exists():
            result = json.loads(path.read_text(encoding="utf-8"))
            if continuous:
                bt._prop_profile = copy.deepcopy(profile)
                bt._stop_on_halt = False
                bt._stop_on_pass = False
                bt._ignore_target_pause = values["ignore_target_pause"]
            for field in ("pass_dt", "halt_dt"):
                value = result.get(field)
                setattr(bt, "prop_" + field, datetime.fromisoformat(value) if value else None)
            logging.getLogger("prop_backtest_1to1").info("Resumed completed phase %s", path.name)
            return result
        result = function(*args, **kwargs)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, allow_nan=False), encoding="utf-8")
        temporary.replace(path)
        logging.getLogger("prop_backtest_1to1").info("Saved completed phase %s", path.name)
        return result

    return wrapped
