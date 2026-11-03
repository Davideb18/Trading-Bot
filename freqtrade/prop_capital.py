"""Operational capital gate shared by initial entries and position increases."""

import json
from datetime import datetime
from pathlib import Path


def prop_capital_available(user_data_dir, now):
    """A missing/stale supervisor never authorizes new risk. Exits are unaffected."""
    try:
        state = json.loads((Path(user_data_dir) / "risk_manager_prop_state.json").read_text())
        updated = datetime.fromisoformat(state["last_updated"])
        return (
            state.get("schema_version") == 1
            and updated.tzinfo is not None
            and 0 <= (now - updated).total_seconds() <= 15
            and state.get("nuclear_triggered") is False
            and state.get("data_fault") is False
            and state.get("wake_up_lock") is False
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False
