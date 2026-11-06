"""P0.6 admission gate. No certified loss/fee model currently exists for NFIX8.

Exposure quantities are inventory, not money-at-risk. Unknown bounds never become
zero. Equity already includes floating P&L; only additional future losses belong
against headroom. This module deliberately cannot grant an entry authorization.
"""

import json
import time
import uuid
from decimal import Decimal

from freqtrade.order_journal import JournalBlocked, quantity
from freqtrade.prop_controls import supervisor_state, sync_prop_controls
from freqtrade.prop_profile import floors, number, profile_from_config


def budget(profile, midnight_equity, equity):
    if not number(equity) or equity < 0:
        raise ValueError("Invalid current equity")
    limits = floors(profile, midnight_equity)
    floor = max(limits["operating_daily_floor"], limits["operating_overall_floor"])
    return {"operating_floor": floor,
            "headroom": float(max(Decimal(0), Decimal(str(equity)) - Decimal(str(floor))))}


def inventory(journal):
    """One pending component per order; fills belong only to actual positions.

    Unobserved intents remain unknown reservations, including ambiguous submits.
    Costs and future position losses are unknown, not a fabricated notional loss.
    """
    snapshot = journal.risk_snapshot
    if snapshot is None:
        raise ValueError("No reconciled exposure snapshot")
    positions = [{"symbol": p["symbol"], "side": p["side"],
                  "amount": quantity(p["amount"])} for p in snapshot["positions"]]
    pending, seen = [], set()
    for order in snapshot["open_orders"]:
        if order.get("reduceOnly") is True:
            continue
        amount, filled = quantity(order["amount"]), quantity(order["filled"])
        if filled > amount:
            raise ValueError("Invalid pending quantity")
        pending.append({"order_id": order["id"], "symbol": order["symbol"],
                        "side": order["side"], "amount": amount - filled})
        seen.add(order["id"])
    unknown = []
    for row in journal.db.execute(
            "SELECT i.* FROM intents i JOIN risk_reservations r USING(client_id) "
            "WHERE r.state='held'"):
        # Matched pending order counted above. A terminal fill is already in
        # positions, but its reservation stays held until full reconciliation.
        if row["exchange_id"] in seen:
            continue
        evidence = snapshot["orders"].get(row["client_id"])
        if evidence and evidence["status"] != "open":
            continue
        unknown.append(row["client_id"])
    return {"positions": positions, "pending_entries": pending,
            "unobserved_reservations": unknown, "future_loss_bound": None,
            "future_cost_bound": None, "floating_pnl_in_equity": True}


def require_risk_budget(bot, request, trade_id=None):
    """Persist a denied intent and its non-consuming reservation atomically.

    No permitted-risk token or configuration escape hatch exists. The unresolved
    model must be implemented and reviewed before this gate can authorize risk.
    Reductions bypass this gate; P0.2-P0.5 still govern their execution safety.
    """
    if request.get("reduceOnly") is True:
        return
    journal = bot.order_journal
    with journal.mutex:
        detail = {"model": "unavailable", "authorized": False}
        try:
            profile = profile_from_config(bot.config)
            binding = json.loads(journal.db.execute("SELECT value FROM binding WHERE id=1").fetchone()[0])
            if any(binding.get(k) != profile[k] for k in (
                    "profile_id", "account_id", "challenge_id", "phase_id",
                    "exchange", "environment", "currency")):
                raise ValueError("Risk identity mismatch")
            state = supervisor_state(bot.config)
            if sync_prop_controls(bot) or not state["data_valid"] or state.get("wake_up_lock") is not False:
                raise ValueError("Risk fault or baseline lock")
            check = journal.last_reconciliation or {}
            if (not journal.ready or check.get("verified") is not True
                    or not 0 <= time.time() * 1000 - check["checked_ms"] <= 15000):
                raise ValueError("Stale or unverified reconciliation")
            if quantity(request["amount"]) <= 0:
                raise ValueError("Invalid requested quantity")
            detail.update(budget(profile, state["baseline"]["equity"], state["last_equity"]))
            detail.update(inventory(journal))
            detail["reason"] = ("operating_headroom_exhausted" if detail["headroom"] == 0
                                else "uncertified_aggregate_loss_and_cost_model")
            # Config/journal identity is checked, but this is not an independent
            # remote UID attestation usable to grant new risk.
            detail["remote_identity_authorization"] = "not_established"
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            detail["reason"] = "unverified_risk_context: " + str(exc)
        cid = "ftj" + uuid.uuid4().hex
        with journal.db:
            journal.db.execute(
                "INSERT INTO intents(client_id,created_ms,request,trade_id,state,settled) "
                "VALUES (?,?,?,?, 'risk_denied',1)",
                (cid, int(time.time() * 1000), json.dumps(request), trade_id))
            journal.db.execute("INSERT INTO risk_reservations VALUES (?, 'denied', ?)",
                               (cid, json.dumps(detail)))
        # Denied is never dispatchable; settled here means never sent, not filled.
        raise JournalBlocked("P0.6 entry denied: " + detail["reason"])
