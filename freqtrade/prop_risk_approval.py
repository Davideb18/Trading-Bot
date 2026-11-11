"""Content-bound review contract. Hashes identify policy; they do not prove human consent."""

import hashlib
import json
from datetime import UTC, datetime

from freqtrade.prop_profile import utc_time


APPROVAL_SCHEMA = 1
OPERATING_KEYS = (
    "trading_mode", "margin_mode", "dry_run", "prop_non_compounding",
    "strategy", "nfi_parameters", "can_short", "position_adjustment_enable",
    "max_open_trades", "stake_amount", "tradable_balance_ratio", "max_stake_amount",
    "order_types", "order_time_in_force", "unfilledtimeout", "timeframe",
)


def policy_identity(profile, policy, config, request):
    """Return an immutable canonical snapshot and its deterministic SHA-256 ID.

    Include every policy parameter (including unknown additions), excluding only
    review metadata. Explicit null distinguishes missing operating settings.
    Never include exchange credentials or unrelated configuration in the journal.
    """
    payload = {
        "approval_schema": APPROVAL_SCHEMA,
        "profile": profile,
        "policy": {k: v for k, v in policy.items() if k not in ("approval", "status")},
        "operating": {k: config.get(k) for k in OPERATING_KEYS},
        "execution": {k: request.get(k) for k in ("leverage", "ordertype", "time_in_force")},
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True, allow_nan=False)
    return canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def reviewed_policy(profile, policy, config, request, detail):
    canonical, fingerprint = policy_identity(profile, policy, config, request)
    detail.update(policy_fingerprint=fingerprint, policy_canonical=canonical,
                  approval_schema_version=APPROVAL_SCHEMA)
    approval = policy.get("approval")
    if not isinstance(approval, dict):
        return False
    # External review is an operator trust boundary, never inferred from a hash.
    # The identifiable reference must be independently checked by the operator.
    required_text = ("reference", "approved_by", "reviewed_at")
    if (policy.get("status") != "approved"
            or approval.get("kind") != "risk_budget_approval"
            or approval.get("reviewed") is not True
            or type(approval.get("schema_version")) is not int
            or approval["schema_version"] != APPROVAL_SCHEMA
            or approval.get("policy_fingerprint") != fingerprint
            or approval.get("account_id") != profile["account_id"]
            or approval.get("challenge_id") != profile["challenge_id"]
            or any(not isinstance(approval.get(k), str) or not approval[k].strip()
                   for k in required_text)):
        return False
    try:
        reviewed = utc_time(approval["reviewed_at"])
        if not utc_time(profile["phase_start_utc"]) <= reviewed <= datetime.now(UTC):
            return False
    except (ValueError, TypeError):
        return False
    detail["approval_evidence"] = {k: approval[k] for k in (
        "schema_version", "policy_fingerprint", "kind", "reviewed", "approved_by",
        "reference", "reviewed_at", "account_id", "challenge_id")}
    return True
