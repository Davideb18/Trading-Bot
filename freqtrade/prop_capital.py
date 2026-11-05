"""Operational capital gate shared by initial entries and position increases."""

import json
from datetime import UTC, datetime
from pathlib import Path

from freqtrade.prop_profile import profile_from_config, validate_baseline


def prop_capital_available(user_data_dir, now, config=None):
    """A missing/stale supervisor never authorizes new risk. Exits are unaffected."""
    try:
        state = json.loads((Path(user_data_dir) / "risk_manager_prop_state.json").read_text())
        profile = profile_from_config(config)
        if state.get("profile") != profile or state.get("risk_schema_version") != 2:
            return False
        validate_baseline(state["baseline"], profile, now)
        if state.get("current_trading_day") != now.astimezone(UTC).date().isoformat():
            return False
        updated = datetime.fromisoformat(state["last_updated"])
        return (
            state.get("schema_version") == 1
            and updated.tzinfo is not None
            and 0 <= (now - updated).total_seconds() <= 15
            and state.get("nuclear_triggered") is False
            and state.get("data_fault") is False
            and state.get("wake_up_lock") is False
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
