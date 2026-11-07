"""P0.6 admission gate with deterministic aggregate notional & stress-headroom bounds.

Exposure quantities are inventory; unknown bounds never become zero. Equity already
includes floating P&L; only additional future losses/costs belong against headroom.
When remote identity attestation and explicit risk budget policy are verified,
orders within aggregate notional and stress-headroom bounds are authorized.
"""

import json
import time
import uuid
from decimal import Decimal

from freqtrade.order_journal import JournalBlocked, quantity
from freqtrade.prop_controls import supervisor_state, sync_prop_controls
from freqtrade.prop_profile import floors, number, profile_from_config


CERTIFIED_BUDGET_MODEL = "hyrotrader_one_step_notional_stress_v1"


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


def _evaluate_certified_budget(bot, profile, state, journal, request, detail):
    """Evaluate aggregate notional cap and stress-loss reserve against operating headroom."""
    policy = bot.config.get("prop_risk_budget")
    if (state.get("remote_identity_verified") is not True
            or not isinstance(policy, dict)
            or policy.get("model") != CERTIFIED_BUDGET_MODEL):
        return False

    detail["remote_identity_authorization"] = "verified"
    ratio = bot.config.get("tradable_balance_ratio", 0.2)
    if not number(ratio) or not 0 < ratio <= 1.0:
        raise ValueError("Invalid tradable_balance_ratio")
    max_theoretical = float(Decimal(str(profile["initial_balance"])) * Decimal(str(ratio)))
    max_notional = policy.get("max_aggregate_notional_usdt", max_theoretical)
    stress_bps = policy.get("stress_loss_bps", 50.0)
    min_buffer = policy.get("min_headroom_buffer_usdt", 25.0)
    if (not number(max_notional) or not 0 < max_notional <= max_theoretical
            or not number(stress_bps) or not 25.0 <= stress_bps <= 5000.0
            or not number(min_buffer) or not 0 <= min_buffer < profile["initial_balance"]):
        raise ValueError("Invalid prop_risk_budget policy parameters")

    if request.get("side") != "buy":
        raise ValueError("Only long entry/DCA orders are permitted by prop_risk_budget")
    lev = request.get("leverage", 1.0)
    if lev is not None and (not number(lev) or float(lev) != 1.0):
        raise ValueError("Leverage must be 1.0x")

    req_rate = request.get("rate")
    if not number(req_rate) or req_rate <= 0:
        raise ValueError("Explicit positive order rate required for risk budget")
    req_qty = quantity(request["amount"])
    req_notional = Decimal(str(req_qty)) * Decimal(str(req_rate))

    snapshot = journal.risk_snapshot
    committed_notional = Decimal(0)
    for order in snapshot["open_orders"]:
        if order.get("reduceOnly") is True:
            continue
        rem = Decimal(str(quantity(order["amount"]))) - Decimal(str(quantity(order["filled"])))
        order_price = order.get("price") or order.get("average")
        if not number(order_price) or order_price <= 0:
            raise ValueError("Unverifiable open order price for risk budget")
        committed_notional += rem * Decimal(str(order_price))

    for pos in snapshot["positions"]:
        pos_qty = Decimal(str(quantity(pos["amount"])))
        pos_price = pos.get("mark_price") or pos.get("price")
        if (not number(pos_price) or pos_price <= 0) and pos.get("symbol") == request.get("pair"):
            pos_price = req_rate
        if not number(pos_price) or pos_price <= 0:
            raise ValueError("Unverifiable position mark price for risk budget")
        committed_notional += pos_qty * Decimal(str(pos_price))

    aggregate_notional = committed_notional + req_notional
    cost_bound = (aggregate_notional * Decimal(str(stress_bps))) / Decimal("10000")
    loss_bound = cost_bound + Decimal(str(min_buffer))

    detail.update(
        model=CERTIFIED_BUDGET_MODEL,
        committed_notional=float(committed_notional),
        requested_notional=float(req_notional),
        aggregate_notional=float(aggregate_notional),
        max_aggregate_notional=float(max_notional),
        future_cost_bound=float(cost_bound),
        future_loss_bound=float(loss_bound),
    )

    if detail["unobserved_reservations"]:
        detail["reason"] = "unobserved_reservations_pending"
        return False
    if aggregate_notional > Decimal(str(max_notional)):
        detail["reason"] = "aggregate_notional_cap_exceeded"
        return False
    if Decimal(str(detail["headroom"])) < loss_bound:
        detail["reason"] = "insufficient_operating_headroom_for_stress_bound"
        return False

    detail["authorized"] = True
    detail["reason"] = "authorized"
    return True


def require_risk_budget(bot, request, trade_id=None):
    """Authorize within verified notional/headroom bounds, or persist a denied intent atomically.

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
            detail["remote_identity_authorization"] = "not_established"
            if detail["headroom"] > 0 and _evaluate_certified_budget(
                    bot, profile, state, journal, request, detail):
                return detail
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
