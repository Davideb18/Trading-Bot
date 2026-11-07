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
