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


def normalized_order_quantities(order, raw=None, *, local_filled=None):
    """Normalize CCXT empty-string terminal quantities without fabricating partial fills."""
    status = order.get("status")
    amount = order.get("amount")
    filled = order.get("filled")
    remaining = order.get("remaining")
    empty_raw_fill = isinstance(raw, dict) and raw.get("cumExecQty") in ("", None)
    local_zero = local_filled is None or local_filled == 0
    if (
        filled is None
        and status in CANCELED_EXCHANGE_STATES
        and (status == "rejected" or empty_raw_fill)
        and local_zero
    ):
        filled = 0.0
    if (
        remaining is None
        and status in CANCELED_EXCHANGE_STATES
        and isinstance(filled, (int, float))
        and type(filled) in (int, float)
        and math.isfinite(filled)
        and filled == 0
    ):
        remaining = 0.0
    return amount, filled, remaining


def expected_order_side(local_order):
    return getattr(local_order, "side", None) or getattr(local_order, "ft_order_side", None)


def _finite_pos(value):
    return (
        isinstance(value, (int, float))
        and type(value) in (int, float)
        and math.isfinite(value)
        and value > 0
    )


def expected_order_amount(local_order, pair=None, exchange=None):
    """Resolve local order base quantity, quantizing ft_amount when createOrder omitted amount."""
    local_amount = getattr(local_order, "amount", None)
    if _finite_pos(local_amount):
        return float(local_amount), True
    ft_amount = getattr(local_order, "ft_amount", None)
    if not _finite_pos(ft_amount):
        return None, False
    if exchange is not None and pair:
        try:
            contracts = exchange._amount_to_contracts(pair, float(ft_amount))
            q_contracts = exchange.amount_to_precision(pair, contracts)
            quantized = exchange._contracts_to_amount(pair, q_contracts)
            if _finite_pos(quantized):
                return float(quantized), True
        except (AttributeError, TypeError, ValueError):
            pass
    return float(ft_amount), False


def _amount_matches_local(exchange_amount, local_order, pair=None, exchange=None):
    expected, exact = expected_order_amount(local_order, pair, exchange)
    if expected is None:
        return False
    if exact:
        return math.isclose(exchange_amount, expected, rel_tol=1e-8, abs_tol=1e-12)
    return 0 < exchange_amount <= expected * (1 + 1e-8)


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
                or not _amount_matches_local(
                    order["amount"], local_order, trade.pair, getattr(bot, "exchange", None)
                )):
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
                or not _amount_matches_local(
                    order["amount"], local_order, trade.pair, getattr(bot, "exchange", None)
                )
                or order["status"] == "open"):
            return None
        return populate_order_quantities(
            dict(order), local_filled=getattr(local_order, "filled", None)
        )
    except (ExchangeError, ValueError, TypeError, KeyError):
        return None


def populate_order_quantities(order, *, raw=None, local_filled=None):
    """Populate missing filled/remaining fields when deterministically known (e.g. Bybit Rejected)."""
    if order.get("filled") is None or order.get("remaining") is None:
        raw_info = raw if isinstance(raw, dict) else (
            order.get("info") if isinstance(order.get("info"), dict) else None
        )
        _, filled, remaining = normalized_order_quantities(
            order, raw=raw_info, local_filled=local_filled
        )
        if order.get("filled") is None and filled is not None:
            order["filled"] = filled
        if order.get("remaining") is None and remaining is not None:
            order["remaining"] = remaining
    return order


def order_evidence_valid(order, local_order, pair):
    """Unknown/missing quantities and regressing fills cannot authorize reconciliation."""
    if not isinstance(order, dict):
        return False
    expected_side = expected_order_side(local_order)
    if (str(order.get("id")) != str(local_order.order_id)
            or order.get("symbol", pair) != pair
            or order.get("side") not in ("buy", "sell")
            or (expected_side is not None and order.get("side") != expected_side)
            or order.get("status") not in ("open", *NON_OPEN_EXCHANGE_STATES)):
        return False
    raw = order.get("info") if isinstance(order.get("info"), dict) else None
    amount, filled, remaining = normalized_order_quantities(
        order, raw=raw, local_filled=getattr(local_order, "filled", None)
    )
    for value in (amount, filled, remaining):
        if (not isinstance(value, (int, float)) or type(value) not in (int, float)
                or not math.isfinite(value) or value < 0):
            return False
    if amount <= 0 or filled > amount:
        return False
    if filled < (local_order.filled or 0):
        return False
    if order["status"] == "closed" and (filled == 0 or remaining != 0):
        return False
    if order["status"] == "open" and not math.isclose(
        filled + remaining, amount, rel_tol=1e-8, abs_tol=1e-12
    ):
        return False
    if filled > 0:
        price = order.get("average") or order.get("price")
        if (not isinstance(price, (int, float)) or type(price) not in (int, float)
                or not math.isfinite(price) or price <= 0):
            return False
    return True
