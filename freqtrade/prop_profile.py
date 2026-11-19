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
    if (p.get("exchange") != "bybit" or p.get("environment") not in ("demo", "live")
            or p.get("currency") != "USDT"):
        raise ValueError("Unsupported exchange/environment/currency profile")
    for key in ("initial_balance", *RISK_KEYS):
        if not number(p.get(key)) or p[key] <= 0:
            raise ValueError(f"Invalid {key}")
    for key in ("challenge_start_utc", "phase_start_utc"):
        p[key] = utc_time(p[key]).isoformat()

    pid = p["profile_id"]
    phase = p["phase_id"]
    if pid == "hyro_one_step":
        if phase != "evaluation":
            raise ValueError("Unsupported One-Step account profile")
        for key, expected in (("official_daily_limit_percent", 0.04),
                              ("profit_target_percent", 0.10),
                              ("daily_profit_cap_percent", 0.40)):
            if p[key] != expected:
                raise ValueError(f"Invalid One-Step {key}")
        if p["official_max_loss_percent"] not in (0.06, 0.08):
            raise ValueError("Invalid One-Step official_max_loss_percent")
        if p["min_trading_days"] not in (3, 4, 5):
            raise ValueError("Invalid One-Step min_trading_days")
        if p["challenge_start_utc"] != p["phase_start_utc"]:
            raise ValueError("One-Step evaluation must start with the challenge")
    elif pid == "hyro_two_step":
        if phase not in ("phase_1", "phase_2", "funded"):
            raise ValueError("Unsupported Two-Step phase_id")
        if p["official_daily_limit_percent"] != 0.05 or p["official_max_loss_percent"] != 0.10:
            raise ValueError("Invalid Two-Step official loss limits (must be 5% daily / 10% overall)")
        if phase == "phase_1":
            if (p["profit_target_percent"] != 0.10 or p["daily_profit_cap_percent"] != 0.40
                    or p["min_trading_days"] not in (4, 5)):
                raise ValueError("Invalid Two-Step Phase 1 target/consistency/min_days")
            if p["challenge_start_utc"] != p["phase_start_utc"]:
                raise ValueError("Two-Step Phase 1 must start with the challenge")
        elif phase == "phase_2":
            if (p["profit_target_percent"] != 0.05 or p["daily_profit_cap_percent"] != 0.40
                    or p["min_trading_days"] not in (4, 5)):
                raise ValueError("Invalid Two-Step Phase 2 target/consistency/min_days")
            if utc_time(p["phase_start_utc"]) < utc_time(p["challenge_start_utc"]):
                raise ValueError("Two-Step Phase 2 cannot start before challenge_start_utc")
        else:
            if (p["profit_target_percent"] not in (0.05, 0.10)
                    or p["daily_profit_cap_percent"] not in (0.40, 1.0)
                    or p["min_trading_days"] not in (1, 3, 4, 5)):
                raise ValueError("Invalid Two-Step Funded parameters")
            if utc_time(p["phase_start_utc"]) < utc_time(p["challenge_start_utc"]):
                raise ValueError("Two-Step Funded cannot start before challenge_start_utc")
    elif pid == "hyro_one_step_funded":
        if phase != "funded":
            raise ValueError("Unsupported One-Step Funded phase_id")
        if (p["official_daily_limit_percent"] != 0.04
                or p["official_max_loss_percent"] not in (0.06, 0.08)
                or p["profit_target_percent"] not in (0.05, 0.10)
                or p["daily_profit_cap_percent"] not in (0.40, 1.0)
                or p["min_trading_days"] not in (1, 3, 4, 5)):
            raise ValueError("Invalid One-Step Funded parameters")
        if utc_time(p["phase_start_utc"]) < utc_time(p["challenge_start_utc"]):
            raise ValueError("One-Step Funded cannot start before challenge_start_utc")
    else:
        raise ValueError("Unsupported account profile")

    if not (p["daily_limit_percent"] < p["official_daily_limit_percent"]
            and p["max_loss_percent"] < p["official_max_loss_percent"]):
        raise ValueError("Operating loss limits must be below official limits")
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
            or evidence.get("account_id") != profile["account_id"]
            or not isinstance(evidence.get("reference"), str) or not evidence["reference"].strip()
            or not number(evidence.get("equity")) or evidence["equity"] != baseline["equity"]
            or utc_time(evidence["observed_at"]) != captured):
        raise ValueError("Reviewed historical midnight equity evidence required")
    kind = evidence.get("kind")
    if kind == "historical_account_equity":
        if evidence.get("reviewed") is not True:
            raise ValueError("Reviewed historical midnight equity evidence required")
    elif kind == "bybit_v5_midnight_equity_reconstruction":
        positions_mid = evidence.get("positions_midnight")
        if (baseline["source"] != "bybit_v5_reconciled_midnight_equity"
                or evidence.get("zero_external_transfers") is not True
                or not number(evidence.get("wallet_balance_midnight"))
                or evidence["wallet_balance_midnight"] <= 0
                or not number(evidence.get("post_midnight_tx_delta"))
                or not isinstance(positions_mid, list)):
            raise ValueError("Incomplete Bybit V5 midnight equity reconstruction")
        upl_total = 0.0
        for pos in positions_mid:
            if (not isinstance(pos, dict)
                    or not isinstance(pos.get("symbol"), str) or not pos["symbol"].strip()
                    or not number(pos.get("size")) or pos["size"] < 0
                    or not number(pos.get("mark_price_midnight")) or pos["mark_price_midnight"] <= 0
                    or not number(pos.get("unrealized_pnl"))):
                raise ValueError("Invalid reconstructed midnight position entry")
            upl_total += float(pos["unrealized_pnl"])
        reconstructed = float(evidence["wallet_balance_midnight"]) + upl_total
        if not math.isclose(reconstructed, float(baseline["equity"]), rel_tol=0.0, abs_tol=1e-4):
            raise ValueError("Reconstructed midnight equity does not match ledger equation")
    else:
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


def build_program_profiles(
    initial_balance: float = 5000.0,
    start_utc: str = "2024-11-15T00:00:00+00:00",
    *,
    one_step_daily_guard: float = 0.036,
    one_step_max_guard: float = 0.065,
    two_step_daily_guard: float = 0.043,
    two_step_max_guard: float = 0.080,
) -> dict[str, dict]:
    """Return validated One-Step, Two-Step (Phase 1 & 2) and Funded profiles."""
    base = {
        "account_id": "backtest_sim_acct",
        "challenge_id": "backtest_sim_chal",
        "initial_balance": float(initial_balance),
        "exchange": "bybit",
        "environment": "demo",
        "currency": "USDT",
        "challenge_start_utc": start_utc,
        "phase_start_utc": start_utc,
    }
    return {
        "one_step_eval": validate_profile({
            **base,
            "profile_id": "hyro_one_step",
            "phase_id": "evaluation",
            "official_daily_limit_percent": 0.04,
            "official_max_loss_percent": 0.08,
            "daily_limit_percent": float(one_step_daily_guard),
            "max_loss_percent": float(one_step_max_guard),
            "profit_target_percent": 0.10,
            "daily_profit_cap_percent": 0.40,
            "min_trading_days": 3,
        }),
        "one_step_funded": validate_profile({
            **base,
            "profile_id": "hyro_one_step_funded",
            "phase_id": "funded",
            "official_daily_limit_percent": 0.04,
            "official_max_loss_percent": 0.08,
            "daily_limit_percent": float(one_step_daily_guard),
            "max_loss_percent": float(one_step_max_guard),
            "profit_target_percent": 0.05,
            "daily_profit_cap_percent": 1.0,
            "min_trading_days": 1,
        }),
        "two_step_phase1": validate_profile({
            **base,
            "profile_id": "hyro_two_step",
            "phase_id": "phase_1",
            "official_daily_limit_percent": 0.05,
            "official_max_loss_percent": 0.10,
            "daily_limit_percent": float(two_step_daily_guard),
            "max_loss_percent": float(two_step_max_guard),
            "profit_target_percent": 0.10,
            "daily_profit_cap_percent": 0.40,
            "min_trading_days": 4,
        }),
        "two_step_phase2": validate_profile({
            **base,
            "profile_id": "hyro_two_step",
            "phase_id": "phase_2",
            "official_daily_limit_percent": 0.05,
            "official_max_loss_percent": 0.10,
            "daily_limit_percent": float(two_step_daily_guard),
            "max_loss_percent": float(two_step_max_guard),
            "profit_target_percent": 0.05,
            "daily_profit_cap_percent": 0.40,
            "min_trading_days": 4,
        }),
        "two_step_funded": validate_profile({
            **base,
            "profile_id": "hyro_two_step",
            "phase_id": "funded",
            "official_daily_limit_percent": 0.05,
            "official_max_loss_percent": 0.10,
            "daily_limit_percent": float(two_step_daily_guard),
            "max_loss_percent": float(two_step_max_guard),
            "profit_target_percent": 0.05,
            "daily_profit_cap_percent": 1.0,
            "min_trading_days": 1,
        }),
    }
