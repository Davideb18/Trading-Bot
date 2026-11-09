"""P0.6 admission gate: validated 5-component loss/cost risk budget for NFIX8.

Exposure quantities are inventory, not money-at-risk. Unknown bounds never become
zero. Equity already includes floating P&L; only additional future losses and fees
belong against headroom.
"""

import json
import math
import time
import uuid
from decimal import Decimal
from typing import Any

from freqtrade.order_journal import JournalBlocked, quantity
from freqtrade.prop_controls import supervisor_state, sync_prop_controls
from freqtrade.prop_profile import floors, number, profile_from_config

CERTIFIED_BUDGET_MODEL = "hyrotrader_one_step_validated_v2"


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


def _evaluate_validated_budget(profile, policy, request, detail, snapshot):
    detail["model"] = CERTIFIED_BUDGET_MODEL
    leverage = request.get("leverage")
    if leverage is not None and (not number(leverage) or not math.isclose(float(leverage), 1.0, rel_tol=1e-9, abs_tol=1e-9)):
        detail["reason"] = "leverage_exceeds_one_step_limit"
        return
    initial = Decimal(str(profile["initial_balance"]))
    required_keys = (
        "max_slot_notional_usdt",
        "max_aggregate_notional_usdt",
        "roundtrip_fee_bps",
        "funding_bound_bps",
        "execution_slippage_bps",
        "latency_gap_stress_bps",
        "min_headroom_buffer_usdt",
    )
    for k in required_keys:
        if not number(policy.get(k)) or float(policy[k]) <= 0:
            raise ValueError(f"Invalid prop_risk_budget parameter: {k}")

    max_slot_notional = Decimal(str(policy["max_slot_notional_usdt"]))
    max_agg_notional = Decimal(str(policy["max_aggregate_notional_usdt"]))
    fee_bps = Decimal(str(policy["roundtrip_fee_bps"]))
    funding_bps = Decimal(str(policy["funding_bound_bps"]))
    slippage_bps = Decimal(str(policy["execution_slippage_bps"]))
    latency_bps = Decimal(str(policy["latency_gap_stress_bps"]))
    min_buffer = Decimal(str(policy["min_headroom_buffer_usdt"]))

    if (max_slot_notional > initial * Decimal("0.15")
            or max_agg_notional > initial * Decimal("0.24")
            or fee_bps < Decimal("12.0")
            or funding_bps < Decimal("10.0")
            or slippage_bps < Decimal("25.0")
            or latency_bps < Decimal("200.0")
            or min_buffer < Decimal("25.0")
            or min_buffer >= initial * Decimal(str(profile["daily_limit_percent"]))):
        raise ValueError("prop_risk_budget policy parameters violate certified safety bounds")

    req_rate = request.get("rate") if request.get("rate") is not None else request.get("price")
    if not number(req_rate) or float(req_rate) <= 0:
        raise ValueError("Positive order rate/price required for notional valuation")
    req_pair = request.get("pair") or request.get("symbol")
    if not isinstance(req_pair, str) or not req_pair.strip():
        raise ValueError("Explicit order pair required")
    req_amount = quantity(request["amount"])
    req_notional = Decimal(str(req_amount)) * Decimal(str(float(req_rate)))

    pair_notional = req_notional
    existing_notional = Decimal("0")
    for pos in snapshot["positions"]:
        amt = quantity(pos["amount"])
        if amt <= 0:
            continue
        mp = pos.get("mark_price")
        if not number(mp) or float(mp) <= 0:
            raise ValueError(f"Missing verified mark_price for open position {pos.get('symbol')}")
        pos_val = Decimal(str(amt)) * Decimal(str(float(mp)))
        existing_notional += pos_val
        if pos.get("symbol") == req_pair:
            pair_notional += pos_val

    pending_notional = Decimal("0")
    for order in snapshot["open_orders"]:
        if order.get("reduceOnly") is True:
            continue
        rem = quantity(order["amount"]) - quantity(order["filled"])
        if rem <= 0:
            continue
        op = order.get("price")
        if not number(op) or float(op) <= 0:
            raise ValueError(f"Missing limit price for open entry order {order.get('id')}")
        ord_val = Decimal(str(rem)) * Decimal(str(float(op)))
        pending_notional += ord_val
        if order.get("symbol") == req_pair:
            pair_notional += ord_val

    aggregate_notional = existing_notional + pending_notional + req_notional
    total_cost_bps = fee_bps + funding_bps
    total_loss_bps = slippage_bps + latency_bps
    future_cost_bound = (aggregate_notional * total_cost_bps) / Decimal("10000")
    future_loss_bound = (aggregate_notional * total_loss_bps) / Decimal("10000")
    required_headroom = future_cost_bound + future_loss_bound + min_buffer

    detail.update(
        requested_notional=float(req_notional),
        pair_notional=float(pair_notional),
        existing_notional=float(existing_notional),
        pending_notional=float(pending_notional),
        aggregate_notional=float(aggregate_notional),
        future_cost_bound=float(future_cost_bound),
        future_loss_bound=float(future_loss_bound),
        loss_bound=float(future_loss_bound + future_cost_bound),
        min_headroom_buffer=float(min_buffer),
        required_headroom=float(required_headroom),
    )

    if pair_notional > max_slot_notional:
        detail["reason"] = "slot_notional_cap_exceeded"
    elif aggregate_notional > max_agg_notional:
        detail["reason"] = "aggregate_notional_cap_exceeded"
    elif required_headroom > Decimal(str(detail["headroom"])):
        detail["reason"] = "insufficient_operating_headroom_for_stress_bound"
    else:
        detail["authorized"] = True
        detail["reason"] = "authorized"


def require_risk_budget(bot, request, trade_id=None):
    """Evaluate P0.6 risk budget and either return an authorized reservation or fail closed.

    Reductions bypass this gate; P0.2-P0.5 still govern their execution safety.
    """
    if request.get("reduceOnly") is True:
        return
    journal = bot.order_journal
    with journal.mutex:
        detail: dict[str, Any] = {"model": "unavailable", "authorized": False}
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
            remote_verified = state.get("remote_identity_verified") is True
            detail["remote_identity_authorization"] = "verified" if remote_verified else "not_established"
            policy = bot.config.get("prop_risk_budget")
            if detail["unobserved_reservations"]:
                detail["reason"] = "unobserved_reservations_pending"
            elif detail["headroom"] == 0:
                detail["reason"] = "operating_headroom_exhausted"
            elif not isinstance(policy, dict) or policy.get("model") != CERTIFIED_BUDGET_MODEL:
                detail["reason"] = "uncertified_aggregate_loss_and_cost_model"
            elif not remote_verified:
                detail["reason"] = "remote_identity_not_verified"
            else:
                _evaluate_validated_budget(profile, policy, request, detail, journal.risk_snapshot)
                if detail.get("authorized") is True:
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
