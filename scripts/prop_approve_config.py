"""Operator commissioning & cryptographic policy-seal helper for HyroTrader One-Step deployments."""

import argparse
import copy
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from freqtrade.prop_profile import profile_from_config, utc_time, validate_baseline
from freqtrade.prop_risk_approval import APPROVAL_SCHEMA, policy_identity, reviewed_policy


def _atomic_write_json(path: Path, payload: dict, *, private: bool = True) -> None:
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=4, allow_nan=False) + "\n", encoding="utf-8")
    if private:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    tmp.replace(path)


def approve_and_bind_config(
    config: dict,
    *,
    account_id: str,
    challenge_id: str,
    environment: str,
    reference: str,
    approved_by: str = "operator",
    phase_start_utc: str | None = None,
    challenge_start_utc: str | None = None,
    now_utc: datetime | None = None,
) -> dict:
    """Bind HyroTrader account/challenge identity and cryptographically seal prop_risk_budget."""
    if environment not in ("demo", "live"):
        raise ValueError("environment must be 'demo' or 'live'")
    if not account_id or not account_id.strip():
        raise ValueError("Non-empty account_id (Bybit UID) is required")
    if not challenge_id or not challenge_id.strip():
        raise ValueError("Non-empty challenge_id is required")
    if not reference or not reference.strip():
        raise ValueError("Non-empty approval reference is required")
    if not approved_by or not approved_by.strip():
        raise ValueError("Non-empty approved_by is required")

    now = (now_utc or datetime.now(UTC)).astimezone(UTC)
    phase_dt = utc_time(phase_start_utc) if phase_start_utc else now
    if phase_dt > now:
        phase_dt = now
    challenge_dt = utc_time(challenge_start_utc) if challenge_start_utc else phase_dt

    updated = copy.deepcopy(config)
    updated["exchange"]["demo_trading"] = (environment == "demo")
    updated.setdefault("order_time_in_force", {"entry": "GTC", "exit": "GTC"})
    updated["prop_guard"].update(
        account_id=account_id.strip(),
        challenge_id=challenge_id.strip(),
        challenge_start_utc=challenge_dt.isoformat(),
        phase_start_utc=phase_dt.isoformat(),
    )

    profile = profile_from_config(updated)
    policy = updated["prop_risk_budget"]
    request = {
        "leverage": float(updated["nfi_parameters"]["futures_mode_leverage"]),
        "ordertype": updated["order_types"]["entry"],
        "time_in_force": updated["order_time_in_force"]["entry"],
    }
    policy["status"] = "approved"
    reviewed_at = max(phase_dt, now).isoformat()
    _, fingerprint = policy_identity(profile, policy, updated, request)
    policy["approval"] = {
        "kind": "risk_budget_approval",
        "reviewed": True,
        "schema_version": APPROVAL_SCHEMA,
        "account_id": profile["account_id"],
        "challenge_id": profile["challenge_id"],
        "reference": reference.strip(),
        "approved_by": approved_by.strip(),
        "reviewed_at": reviewed_at,
        "policy_fingerprint": fingerprint,
    }

    detail: dict = {}
    if not reviewed_policy(profile, policy, updated, request, detail):
        raise ValueError("Failed to verify sealed prop_risk_budget approval")
    return updated


def build_flat_initial_baseline(
    profile: dict,
    *,
    equity: float,
    reference: str,
    now_utc: datetime | None = None,
) -> dict:
    """Create a validated Day-1 flat starting baseline record requested explicitly by operator."""
    now = (now_utc or datetime.now(UTC)).astimezone(UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    record = {
        "profile": profile,
        "confirmed": True,
        "status": "verified",
        "source": "operator_initial_account_equity",
        "captured_at": midnight,
        "verified_at": now.isoformat(),
        "equity": float(equity),
        "evidence": {
            "kind": "historical_account_equity",
            "reviewed": True,
            "account_id": profile["account_id"],
            "reference": reference.strip(),
            "equity": float(equity),
            "observed_at": midnight,
        },
    }
    return validate_baseline(record, profile, now)


def acquire_bybit_baseline(config: dict, profile: dict, *, api_key: str, api_secret: str) -> dict:
    """Fetch verified Bybit V5 midnight baseline using SupervisorBybit."""
    import ccxt
    from freqtrade.supervisor_bybit import SupervisorBybit

    api = ccxt.bybit({
        "apiKey": api_key,
        "secret": api_secret,
        "timeout": 4000,
        "enableRateLimit": True,
        "maxRetriesOnFailure": 0,
        "options": {"defaultType": "swap", "defaultSubType": "linear", "defaultSettle": "USDT"},
    })
    api.enable_demo_trading(profile["environment"] == "demo")
    sup = SupervisorBybit(api, profile)
    with sup.operation_budget(12.0):
        baseline = sup.reconcile_midnight_baseline()
    return validate_baseline(baseline, profile, datetime.now(UTC))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Commission & seal HyroTrader One-Step prop config.")
    parser.add_argument("--config", default=str(ROOT / "user_data" / "config_prop_nfix8.json"))
    parser.add_argument("--account-id", required=True, help="Bybit account UID")
    parser.add_argument("--challenge-id", required=True, help="HyroTrader challenge ID")
    parser.add_argument("--environment", required=True, choices=("demo", "live"))
    parser.add_argument("--reference", required=True, help="Operator audit approval reference")
    parser.add_argument("--approved-by", default="operator")
    parser.add_argument("--phase-start-utc", default=None)
    parser.add_argument("--challenge-start-utc", default=None)
    parser.add_argument("--state-dir", default=None, help="Directory for baseline/state files")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--initial-flat-equity", type=float, default=None,
                       help="Write Day-1 reviewed baseline for a verified flat account balance")
    group.add_argument("--from-bybit", action="store_true",
                       help="Acquire baseline directly via Bybit V5 transaction log & wallet")
    args = parser.parse_args(argv)

    cfg_path = Path(args.config).resolve()
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    approved = approve_and_bind_config(
        raw,
        account_id=args.account_id,
        challenge_id=args.challenge_id,
        environment=args.environment,
        reference=args.reference,
        approved_by=args.approved_by,
        phase_start_utc=args.phase_start_utc,
        challenge_start_utc=args.challenge_start_utc,
    )
    _atomic_write_json(cfg_path, approved)

    profile = profile_from_config(approved)
    state_dir = Path(args.state_dir).resolve() if args.state_dir else cfg_path.parent
    baseline_written = None
    if args.from_bybit:
        key = os.environ.get("PROP_BYBIT_API_KEY") or approved["exchange"].get("key") or ""
        secret = os.environ.get("PROP_BYBIT_API_SECRET") or approved["exchange"].get("secret") or ""
        if not key or not secret:
            raise ValueError("PROP_BYBIT_API_KEY and PROP_BYBIT_API_SECRET required for --from-bybit")
        baseline = acquire_bybit_baseline(approved, profile, api_key=key, api_secret=secret)
        b_path = state_dir / "risk_manager_prop_baseline.json"
        _atomic_write_json(b_path, baseline)
        baseline_written = str(b_path)
    elif args.initial_flat_equity is not None:
        baseline = build_flat_initial_baseline(
            profile, equity=args.initial_flat_equity, reference=args.reference,
        )
        b_path = state_dir / "risk_manager_prop_baseline.json"
        _atomic_write_json(b_path, baseline)
        baseline_written = str(b_path)

    print(json.dumps({
        "config": str(cfg_path),
        "environment": profile["environment"],
        "account_id": profile["account_id"],
        "challenge_id": profile["challenge_id"],
        "policy_fingerprint": approved["prop_risk_budget"]["approval"]["policy_fingerprint"],
        "baseline_written": baseline_written,
        "next_step": "Run `python3 scripts/prop_secure_start.py commission` before starting compose stack.",
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

