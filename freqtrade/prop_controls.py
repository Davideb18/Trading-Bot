"""Persistent operational interlocks in the P0.3 journal, not a second execution store."""

import json
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from freqtrade.order_journal import JournalBlocked
from freqtrade.order_recovery import pending_recovery
from freqtrade.prop_profile import profile_from_config, utc_time, validate_baseline


FLAGS = ("macro_paused", "data_fault", "risk_halt", "manual_lock")


def publish_fault_event(directory, profile, reason):
    """Secondary workers may only append fault signals, never mutate order intents.

    Separate SQLite connection, 50 ms busy timeout, no shared in-process lock.
    If storage is unavailable the caller still latches memory and the heartbeat
    gate remains fail-closed. No software can promise durability on failed storage.
    """
    path = Path(directory) / "order_journal" / "orders.sqlite"
    connection = None
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=0.05)
        connection.execute("PRAGMA synchronous=FULL")
        binding = json.loads(connection.execute("SELECT value FROM binding WHERE id=1").fetchone()[0])
        if any(binding.get(k) != profile[k] for k in ("account_id", "challenge_id", "phase_id", "profile_id")):
            return False
        with connection:
            connection.execute("INSERT INTO fault_events(event_id,created_ms,reason) VALUES (?,?,?)",
                (uuid.uuid4().hex, int(time.time() * 1000), str(reason)[:500]))
        return True
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        return False
    finally:
        if connection is not None:
            connection.close()


def read_control(journal):
    with journal.mutex:
        row = journal.db.execute("SELECT value FROM control WHERE id=1").fetchone()
        return json.loads(row[0]) if row else {
            **dict.fromkeys(FLAGS, False), "epoch": uuid.uuid4().hex,
            "fault_token": None, "ack": None,
        }


def write_control(journal, state, event):
    # Called under the journal mutex. Audit and state have one SQLite commit.
    with journal.db:
        journal.db.execute("INSERT OR REPLACE INTO control VALUES (1,?)", (json.dumps(state),))
        journal.db.execute("INSERT INTO control_audit VALUES (?,?,?)",
            (uuid.uuid4().hex, int(time.time() * 1000), json.dumps({"event": event, "state": state})))


def _read_supervisor_state_raw(config, now=None):
    now = now or datetime.now(UTC)
    state = json.loads((Path(config["user_data_dir"]) / "risk_manager_prop_state.json").read_text())
    profile = profile_from_config(config)
    updated = utc_time(state["last_updated"])
    if (state.get("profile") != profile or state.get("risk_schema_version") != 2
            or state.get("control_schema_version") != 1
            or not 0 <= (now - updated).total_seconds() <= 15):
        raise ValueError("Missing, stale or mismatched supervisor control state")
    for key in ("data_fault", "nuclear_triggered", "macro_paused", "data_valid"):
        if type(state.get(key)) is not bool:
            raise ValueError("Invalid supervisor control flags")
    return state, profile, updated, now


def supervisor_state(config, now=None):
    state, profile, _updated, check_now = _read_supervisor_state_raw(config, now)
    if state["data_valid"]:
        validate_baseline(state["baseline"], profile, check_now)
    return state


def sync_prop_controls(bot, *, allow_ack=False):
    if not getattr(bot, "config", {}).get("prop_guard_enabled"):
        return False
    journal = getattr(bot, "order_journal", None)
    if journal is None:
        return True
    rollover_pending = False
    try:
        source, profile, updated, check_now = _read_supervisor_state_raw(bot.config)
        if source["data_valid"]:
            try:
                validate_baseline(source["baseline"], profile, check_now)
            except ValueError:
                # Immediately after 00:00 UTC, core can read yesterday's still-fresh (<=15s) state
                # before supervisor finishes autonomous rollover; pause entries without sticky data_fault.
                if (updated.astimezone(UTC).date() < check_now.astimezone(UTC).date()
                        and not source["data_fault"] and not source["nuclear_triggered"]):
                    validate_baseline(source["baseline"], profile, updated)
                    rollover_pending = True
                else:
                    raise
        transient_only = (
            (source.get("transient_paused") is True or rollover_pending)
            and not source["data_fault"]
            and not source["nuclear_triggered"]
        )
        fault = source["data_fault"] or (
            (not source["data_valid"] or rollover_pending) and not transient_only
        )
        token = str(source.get("fault_revision", 0)) if fault else None
        error = None
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        source, fault, token, error, rollover_pending = {}, True, "unavailable", type(exc).__name__, False
    with journal.mutex:
        state = read_control(journal)
        before = json.dumps(state, sort_keys=True)
        events = [r[0] for r in journal.db.execute(
            "SELECT event_id FROM fault_events WHERE acknowledged=0 ORDER BY rowid")]
        if events and state.get("event_token") != events[-1]:
            state.update(data_fault=True, event_token=events[-1], epoch=uuid.uuid4().hex, ack=None)
        if fault and (not state["data_fault"] or state["fault_token"] != token):
            state.update(data_fault=True, fault_token=token, epoch=uuid.uuid4().hex, ack=None)
        if source.get("nuclear_triggered"):
            state["risk_halt"] = True
        if source:
            state["macro_paused"] = bool(
                source["macro_paused"]
                or source.get("rules_paused")
                or source.get("transient_paused")
                or rollover_pending
            )
            ack = state.get("ack")
            # Two-phase acknowledgement: authenticated RPC requests; the supervisor
            # validates fresh data; the core validates its unchanged fault epoch.
            if (allow_ack and not fault and not rollover_pending and source.get("data_valid") is True
                    and not state["risk_halt"] and ack
                    and source.get("acknowledged_control") == ack["id"]
                    and ack["epoch"] == state["epoch"]
                    and 0 <= time.time() * 1000 - ack["created_ms"] <= 60000):
                for flag in ack["flags"]:
                    state[flag] = False
                if "data_fault" in ack["flags"]:
                    with journal.db:
                        journal.db.executemany("UPDATE fault_events SET acknowledged=1 WHERE event_id=?",
                                               [(e,) for e in ack.get("events", [])])
                state["ack"] = None
        state["source_error"] = error
        if json.dumps(state, sort_keys=True) != before:
            write_control(journal, state, "supervisor_sync")
        return any(state[k] for k in FLAGS)


def control_status(bot, trade_model, order_model):
    sync_prop_controls(bot)
    journal = getattr(bot, "order_journal", None)
    if journal is None:
        raise JournalBlocked("Prop journal unavailable")
    with journal.mutex:
        state = read_control(journal)
        health = journal.last_reconciliation or {}
        fresh = 0 <= time.time() * 1000 - health.get("checked_ms", 0) <= 15000
        try:
            from freqtrade.snapshot_freshness import validate_snapshot
            validate_snapshot(journal.risk_snapshot)
        except (ValueError, TypeError, KeyError, AttributeError):
            fresh = False
        trades = trade_model.get_open_trades()
        local_pending = any(pending_recovery(t) for t in trades)
        pending_entries = any(o.side == t.entry_side for t in trades for o in t.open_orders)
        orphan = any(o.trade is None for o in order_model.get_open_orders())
        cancellation_pending = journal.db.execute(
            "SELECT 1 FROM cancellations WHERE state!='confirmed' LIMIT 1").fetchone() is not None
        verified = bool(journal.ready and health.get("verified") and fresh
                        and not local_pending and not orphan and not cancellation_pending)
        if verified and not pending_entries:
            sync_prop_controls(bot, allow_ack=True)
            state = read_control(journal)
        return {**state, "reconciliation_required": not verified, "verified": verified,
                "pending_entries": pending_entries, "flat": verified and health.get("flat") is True,
                "checked_ms": health.get("checked_ms")}


def request_control(bot, trade_model, order_model, action, reason, flags=None):
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise JournalBlocked("An explicit operator reason is required")
    journal = bot.order_journal
    with journal.mutex:
        status = control_status(bot, trade_model, order_model)
        state = read_control(journal)
        if action == "lock":
            state.update(manual_lock=True, epoch=uuid.uuid4().hex, ack=None)
        elif action == "fault":
            state.update(data_fault=True, fault_token="rpc_fault", epoch=uuid.uuid4().hex, ack=None)
        elif action == "acknowledge":
            flags = flags or ["data_fault"]
            if (not isinstance(flags, list) or not flags
                    or any(f not in ("data_fault", "manual_lock") for f in flags)):
                raise JournalBlocked("Only data_fault/manual_lock can be acknowledged")
            source = supervisor_state(bot.config)
            if (state["risk_halt"] or source["nuclear_triggered"] or not source["data_valid"]
                    or not status["verified"] or status["pending_entries"]):
                raise JournalBlocked("Fresh data and completed reconciliation are required")
            state["ack"] = {"id": uuid.uuid4().hex, "epoch": state["epoch"], "flags": flags,
                            "created_ms": int(time.time() * 1000), "reason": reason,
                            "authority": "authenticated_rpc", "events": [r[0] for r in journal.db.execute(
                                "SELECT event_id FROM fault_events WHERE acknowledged=0 ORDER BY rowid")]}
        else:
            raise JournalBlocked("Unknown control action")
        write_control(journal, state, {"action": action, "reason": reason})
        return {"status": "persisted", "epoch": state["epoch"], "ack": state["ack"]}
