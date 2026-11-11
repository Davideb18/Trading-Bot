"""Exchange-independent evidence checks and persistent order-recovery markers.

Markers refer to known local trades/orders. They are not an order-intent journal.
"""

import math

from freqtrade.constants import CANCELED_EXCHANGE_STATES, NON_OPEN_EXCHANGE_STATES


RECOVERY_KEY = "core_order_recovery_pending"
UNKNOWN_FORCE_EXIT = "force_exit_submission_unknown"


def pending_recovery(trade):
    return trade.get_custom_data(key=RECOVERY_KEY, default=[])


def mark_recovery(trade, order_id):
    pending = set(pending_recovery(trade))
    pending.add(str(order_id))
    trade.set_custom_data(key=RECOVERY_KEY, value=sorted(pending))


def clear_recovery(trade, order_id):
    pending = set(pending_recovery(trade))
    if str(order_id) in pending:
        pending.remove(str(order_id))
        trade.set_custom_data(key=RECOVERY_KEY, value=sorted(pending))


def verified_prop_cancel(bot, trade, local_order, trade_model, *, terminal_evidence=None):
    """Cancel only with core authority; a fresh lookup, never an ACK, proves the outcome.

    Keep the existing recovery marker until the caller applies terminal evidence
    to the ORM. An open or unknown result remains retry-blocking, not canceled.
    """
    from freqtrade.emergency_authority import require_core_authority
    from freqtrade.exceptions import ExchangeError

    require_core_authority(bot)
    mark_recovery(trade, local_order.order_id)
    trade_model.commit()
    try:
        # Force-exit/fault recovery already performed a verified terminal lookup.
        # Reuse that evidence rather than adding another network dependency to reduction.
        order = (terminal_evidence if terminal_evidence is not None
                 else bot.exchange.fetch_order(local_order.order_id, trade.pair))
        if (not order_evidence_valid(order, local_order, trade.pair)
                or order.get("symbol") != trade.pair
                or not math.isclose(order["amount"], local_order.amount, rel_tol=1e-8, abs_tol=1e-12)):
            return None
        if terminal_evidence is not None and order["status"] == "open":
            return None
        if order["status"] == "open":
            require_core_authority(bot)
            try:
                bot.exchange.cancel_order(local_order.order_id, trade.pair)
            except ExchangeError:
                pass  # A failed acknowledgement cannot decide whether cancellation happened.
            order = bot.exchange.fetch_order(local_order.order_id, trade.pair)
        if (not order_evidence_valid(order, local_order, trade.pair)
                or order.get("symbol") != trade.pair
                or not math.isclose(order["amount"], local_order.amount, rel_tol=1e-8, abs_tol=1e-12)
                or order["status"] == "open"):
            return None
        return order
    except (ExchangeError, ValueError, TypeError, KeyError):
        return None


def order_evidence_valid(order, local_order, pair):
    """Unknown/missing quantities and regressing fills cannot authorize reconciliation."""
    if not isinstance(order, dict):
        return False
    if (str(order.get("id")) != str(local_order.order_id)
            or order.get("symbol", pair) != pair
            or order.get("side") != local_order.side
            or order.get("status") not in ("open", *NON_OPEN_EXCHANGE_STATES)):
        return False
    for key in ("amount", "filled", "remaining"):
        value = order.get(key)
        if (
            key == "remaining"
            and value is None
            and order.get("status") in CANCELED_EXCHANGE_STATES
            and order.get("filled") == 0
        ):
            value = 0.0
        if (not isinstance(value, (int, float)) or type(value) not in (int, float)
                or not math.isfinite(value) or value < 0):
            return False
    if order["amount"] <= 0 or order["filled"] > order["amount"]:
        return False
    if order["filled"] < (local_order.filled or 0):
        return False
    if order["status"] == "closed" and (order["filled"] == 0 or order["remaining"] != 0):
        return False
    if order["status"] == "open" and not math.isclose(
        order["filled"] + order["remaining"], order["amount"], rel_tol=1e-8, abs_tol=1e-12
    ):
        return False
    if order["filled"] > 0:
        price = order.get("average") or order.get("price")
        if (not isinstance(price, (int, float)) or type(price) not in (int, float)
                or not math.isfinite(price) or price <= 0):
            return False
    return True
