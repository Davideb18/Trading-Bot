"""Cancel only verified risk-increasing orders, using the existing P0.2/P0.3 recovery."""

import json
import logging
from types import SimpleNamespace

from freqtrade.constants import NON_OPEN_EXCHANGE_STATES
from freqtrade.exceptions import ExchangeError
from freqtrade.order_recovery import clear_recovery, mark_recovery, order_evidence_valid
from freqtrade.prop_controls import read_control, sync_prop_controls
from freqtrade.emergency_authority import require_core_authority


logger = logging.getLogger(__name__)


def cancel_fault_entries(bot, trade_model):
    require_core_authority(bot)
    if not getattr(bot, "config", {}).get("prop_guard_enabled"):
        return
    sync_prop_controls(bot)
    journal = bot.order_journal
    state = read_control(journal)
    # A macro pause also withdraws pending entries; protective/reducing orders stay.
    blocked = any(state[k] for k in ("data_fault", "risk_halt", "manual_lock", "macro_paused"))
    if not blocked:
        return
    # This is the existing execution lock, not the supervisor risk-state lock.
    with bot._exit_lock:
        trades = trade_model.get_trades_proxy()
        local = {o.order_id: (t, o) for t in trades for o in t.orders}
        candidates = {o.order_id: (t.pair, o.side, None) for t in trades for o in t.open_orders
                      if o.side == t.entry_side}
        for intent in journal.intents():
            request = json.loads(intent["request"])
            if (not request["reduceOnly"] and intent["exchange_id"]
                    and (intent["exchange_id"] not in local
                         or local[intent["exchange_id"]][1].ft_is_open)):
                candidates[intent["exchange_id"]] = (request["pair"], request["side"], intent["client_id"])
        with journal.mutex:
            for row in journal.db.execute("SELECT * FROM cancellations WHERE state!='confirmed'"):
                candidates.setdefault(row["order_id"], (row["pair"], row["side"], None))
        for oid, (pair, side, cid) in candidates.items():
            trade, local_order = local.get(oid, (None, None))
            with journal.mutex, journal.db:
                prior = journal.db.execute("SELECT state FROM cancellations WHERE order_id=?", (oid,)).fetchone()
                if prior and prior[0] == "confirmed" and (local_order is None or not local_order.ft_is_open):
                    continue
                journal.db.execute("INSERT OR IGNORE INTO cancellations VALUES (?,?,?,'requested',NULL)",
                                   (oid, pair, side))
                journal.ready = False
            if trade:
                mark_recovery(trade, oid)
                trade_model.commit()
            try:
                order = bot.exchange.fetch_order(oid, pair)
                reference = local_order or SimpleNamespace(order_id=oid, side=side, filled=0)
                if (not order_evidence_valid(order, reference, pair)
                        or order.get("reduceOnly") is not False
                        or (cid and order.get("clientOrderId") != cid)):
                    raise ValueError("Entry identity/quantities not verified")
                if order["status"] == "open":
                    try:
                        bot.exchange.cancel_order(oid, pair)
                    except ExchangeError:
                        pass  # A lost cancellation response still requires a fresh lookup.
                    order = bot.exchange.fetch_order(oid, pair)
                if (not order_evidence_valid(order, reference, pair)
                        or order["status"] not in NON_OPEN_EXCHANGE_STATES
                        or order.get("reduceOnly") is not False
                        or (cid and order.get("clientOrderId") != cid)):
                    raise ValueError("Cancellation outcome remains uncertain")
                if trade:
                    clear_recovery(trade, oid)
                    already_applied = (not local_order.ft_is_open
                                       and local_order.status == order["status"]
                                       and local_order.filled == order["filled"])
                    if already_applied:
                        pass
                    elif order["status"] != "closed":
                        # Existing P0.2 handler accounts partial fills and empty entries.
                        bot.handle_cancel_enter(trade, order, local_order, "prop_operational_fault")
                    else:
                        bot.update_trade_state(trade, oid, order)
                    trade_model.commit()
                with journal.mutex, journal.db:
                    journal.db.execute("UPDATE cancellations SET state='confirmed',evidence=? WHERE order_id=?",
                                       (json.dumps(order), oid))
            except (ExchangeError, ValueError, KeyError, TypeError) as exc:
                if trade:
                    mark_recovery(trade, oid)
                    trade_model.commit()
                with journal.mutex, journal.db:
                    journal.db.execute("UPDATE cancellations SET state='uncertain' WHERE order_id=?", (oid,))
                logger.warning("Fault cancellation unresolved for %s (%s)", oid, type(exc).__name__)
        # The caller performs P0.3 reconciliation afterwards. A terminal cancel
        # does not itself prove that partial fills are represented in the local DB.
