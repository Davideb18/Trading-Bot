"""Single-host emergency handoff. OS ownership is never a distributed lease."""

import json
import sqlite3
from pathlib import Path

from freqtrade.exceptions import FreqtradeException


class EmergencyTakeover(FreqtradeException):
    """Exit the worker (not merely STOPPED); OS ownership ends at process death."""


def read_emergency(directory):
    connection = sqlite3.connect((Path(directory) / "orders.sqlite").resolve().as_uri() + "?mode=rw",
                                 uri=True, timeout=0.05)
    try:
        row = connection.execute("SELECT value FROM emergency WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None
    finally:
        connection.close()


def require_core_authority(bot):
    if not getattr(bot, "config", {}).get("prop_guard_enabled"):
        return
    journal = getattr(bot, "order_journal", None)
    if journal is None:
        return  # Constructor validates the binding before any exchange initialization.
    with journal.mutex:
        row = journal.db.execute("SELECT value FROM emergency WHERE id=1").fetchone()
    if row and json.loads(row[0]).get("handoff") is True:
        bot._emergency_handoff = True
        raise EmergencyTakeover("Emergency authority requested; core must exit without further orders")
