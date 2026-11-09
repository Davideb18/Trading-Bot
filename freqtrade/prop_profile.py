"""Validated, non-secret One-Step risk contract shared by bot and supervisor."""

import math
import os
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation


IDENTITY_KEYS = ("profile_id", "account_id", "challenge_id", "phase_id")
RISK_KEYS = (
    "official_daily_limit_percent", "official_max_loss_percent", "daily_limit_percent",
    "max_loss_percent", "profit_target_percent", "daily_profit_cap_percent", "min_trading_days",
)
ENV_KEYS = {
    "PROP_FIRM_INITIAL_BALANCE": "initial_balance",
    "GLOBAL_HARD_STOPLOSS_PERCENT": "daily_limit_percent",
    "PROP_FIRM_DAILY_LIMIT_PERCENT": "daily_limit_percent",
    "PROP_FIRM_MAX_LOSS_PERCENT": "max_loss_percent",
    "PROP_FIRM_PROFIT_TARGET_PERCENT": "profit_target_percent",
    "PROP_FIRM_DAILY_PROFIT_CAP_PERCENT": "daily_profit_cap_percent",
    "PROP_FIRM_MIN_TRADING_DAYS": "min_trading_days",
}


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def utc_time(value):
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        raise ValueError("Timezone required")
    return timestamp.astimezone(UTC)


def validate_profile(profile, environ=None):
    """Reject incomplete identity, unsupported profiles and conflicting legacy overrides."""
    p = dict(profile)
    for key in IDENTITY_KEYS:
        if not isinstance(p.get(key), str) or not p[key].strip():
            raise ValueError(f"Explicit {key} required")
    if (p["profile_id"] != "hyro_one_step" or p["phase_id"] != "evaluation"
            or p.get("exchange") != "bybit" or p.get("environment") != "demo"
            or p.get("currency") != "USDT"):
        raise ValueError("Unsupported One-Step account profile")
    for key in ("initial_balance", *RISK_KEYS):
        if not number(p.get(key)) or p[key] <= 0:
            raise ValueError(f"Invalid {key}")
    for key, expected in (("official_daily_limit_percent", 0.04),
                          ("official_max_loss_percent", 0.06),
                          ("profit_target_percent", 0.10),
                          ("daily_profit_cap_percent", 0.40), ("min_trading_days", 5)):
        if p[key] != expected:
            raise ValueError(f"Invalid One-Step {key}")
    if not (p["daily_limit_percent"] < p["official_daily_limit_percent"]
            and p["max_loss_percent"] < p["official_max_loss_percent"]):
        raise ValueError("Operating loss limits must be below official limits")
    for key in ("challenge_start_utc", "phase_start_utc"):
        p[key] = utc_time(p[key]).isoformat()
    if p["challenge_start_utc"] != p["phase_start_utc"]:
        raise ValueError("One-Step evaluation must start with the challenge")
    env = os.environ if environ is None else environ
    for name, key in ENV_KEYS.items():
        if name in env:
            try:
                matches = Decimal(env[name]) == Decimal(str(p[key]))
            except InvalidOperation:
                matches = False
            if not matches:
                raise ValueError(f"Conflicting legacy override: {name}")
    if "PROP_FIRM_PHASE" in env and env["PROP_FIRM_PHASE"] != p["phase_id"]:
        raise ValueError("Conflicting legacy override: PROP_FIRM_PHASE")
    return p


def profile_from_config(config, environ=None):
    """Use resolved Freqtrade config; never expose credentials through the risk contract."""
    if (config.get("prop_guard_enabled") is not True
            or config.get("prop_non_compounding") is not True
            or config.get("trading_mode") != "futures"
            or config.get("margin_mode") != "isolated"
            or config.get("dry_run") is not False or "available_capital" in config):
        raise ValueError("Invalid One-Step deployment configuration")
    raw = config["prop_guard"]
    p = {key: raw[key] for key in (*IDENTITY_KEYS, *RISK_KEYS,
                                  "challenge_start_utc", "phase_start_utc")}
    p.update(initial_balance=config["prop_initial_balance"],
             exchange=config["exchange"]["name"], currency=config["stake_currency"],
             environment="demo" if config["exchange"].get("demo_trading") is True else "live")
    return validate_profile(p, environ)


def validate_baseline(baseline, profile, now):
    """An explicit reconciled midnight record is required; polling cannot reconstruct it."""
    if (baseline["profile"] != profile or baseline.get("confirmed") is not True
            or not isinstance(baseline.get("source"), str) or not baseline["source"].strip()
            or not number(baseline.get("equity")) or baseline["equity"] <= 0):
        raise ValueError("Unverified midnight baseline")
    captured = utc_time(baseline["captured_at"])
    verified = utc_time(baseline["verified_at"])
    if (captured != captured.replace(hour=0, minute=0, second=0, microsecond=0)
            or captured.date() != now.astimezone(UTC).date()
            or not captured <= verified <= now
            or utc_time(profile["phase_start_utc"]) > now):
        raise ValueError("Baseline must describe this UTC midnight and an active phase")
    evidence = baseline.get("evidence")
    # An explicit reviewed historical record is an external trust boundary, not
    # proof reconstructed by polling. A manually entered scalar/confirmed flag,
    # a flat current wallet or a pre/post bracket cannot satisfy this contract.
    if (baseline.get("status") != "verified"
            or baseline["source"] in ("bybit_v5_midnight_conservative_bracket",
                                      "bybit_v5_flat_account_verified",
                                      "bybit_wallet_coin_equity_midnight_rollover")
            or not isinstance(evidence, dict)
            or evidence.get("kind") != "historical_account_equity"
            or evidence.get("reviewed") is not True
            or evidence.get("account_id") != profile["account_id"]
            or not isinstance(evidence.get("reference"), str) or not evidence["reference"].strip()
            or not number(evidence.get("equity")) or evidence["equity"] != baseline["equity"]
            or utc_time(evidence["observed_at"]) != captured):
        raise ValueError("Reviewed historical midnight equity evidence required")
    return baseline


def floors(profile, midnight_equity):
    initial = Decimal(str(profile["initial_balance"]))
    midnight = Decimal(str(midnight_equity))
    return {
        "official_daily_floor": float(midnight - initial * Decimal(str(profile["official_daily_limit_percent"]))),
        "official_overall_floor": float(initial * (1 - Decimal(str(profile["official_max_loss_percent"])))),
        "operating_daily_floor": float(midnight - initial * Decimal(str(profile["daily_limit_percent"]))),
        "operating_overall_floor": float(initial * (1 - Decimal(str(profile["max_loss_percent"])))),
    }
