"""Freqtrade integration for the durable order journal; no second execution path."""

import hashlib
import logging
from pathlib import Path

from freqtrade.order_journal import JournalBlocked, OrderJournal
from freqtrade.order_recovery import clear_recovery, mark_recovery, pending_recovery
from freqtrade.prop_profile import profile_from_config


logger = logging.getLogger(__name__)


def open_journal(config, exchange_config):
    if not config.get("prop_guard_enabled"):
        return None
    profile = profile_from_config(config)
    key = exchange_config.get("api_key", exchange_config.get("apiKey", exchange_config.get("key")))
    if not isinstance(key, str) or not key:
        raise JournalBlocked("Explicit API key required for journal binding")
    for name in ("ccxt_config", "ccxt_sync_config", "ccxt_async_config"):
        override = exchange_config.get(name, {}).get("apiKey", key)
        if override != key:
            raise JournalBlocked("Conflicting CCXT account credentials")
    # Credential fingerprint catches accidental key/account swaps; it is not proof
    # of remote account ownership. The dedicated-account deployment remains required.
    identity = {k: profile[k] for k in ("profile_id", "account_id", "challenge_id",
                                       "phase_id", "exchange", "environment", "currency")}
    identity.update(key_fingerprint=hashlib.sha256(key.encode()).hexdigest(),
                    database=config["db_url"])
    return OrderJournal(Path(config["user_data_dir"]) / "order_journal", identity)


def reconcile_journal(bot, trade_model):
    journal = getattr(bot, "order_journal", None)
    if journal is None:
        return
    with journal.mutex:
        journal.ready = False
        trades = trade_model.get_trades_proxy()
        orders, positions = {}, {}
        for trade in trades:
            for order in trade.orders:
                orders[order.order_id] = {
                    "symbol": trade.pair, "side": order.side, "amount": order.amount,
                    "filled": order.filled or 0, "status": order.status, "trade_id": trade.id,
                }
            if trade.is_open:
                key = (trade.pair, "short" if trade.is_short else "long")
                positions[key] = positions.get(key, 0) + trade.amount
        try:
            snapshot = bot.exchange.fetch_journal_snapshot(journal.intents())
        except Exception as e:
            # Do not log raw exception text: CCXT exceptions can contain request data.
            logger.warning("Journal snapshot unavailable (%s)", type(e).__name__)
            snapshot = {"complete": False}
        errors = journal.reconcile(snapshot, orders, positions)
        if errors:
            logger.warning("Journal blocks entries: %s", errors)
        # Clear only our markers, never P0.2's unknown-force-exit or other order IDs.
        by_id = {t.id: t for t in trades}
        for intent in journal.intents(active=False):
            trade = by_id.get(intent["trade_id"])
            if trade and (intent["settled"] or (intent["linked"] and not errors)):
                clear_recovery(trade, "journal:" + intent["client_id"])
        trade_model.commit()


def journal_create_order(bot, trade, trade_model, **request):
    journal = getattr(bot, "order_journal", None)
    if journal is None:
        if bot.config.get("prop_guard_enabled"):
            raise JournalBlocked("Missing required order journal")
        return bot.exchange.create_order(**request)
    with journal.mutex:
        reconcile_journal(bot, trade_model)
        if ((not request["reduceOnly"] and bot.has_unresolved_orders())
                or (trade and pending_recovery(trade))):
            raise JournalBlocked("P0.2 recovery lock still active")
        cid = journal.prepare(request, trade.id if trade else None)
        try:
            return journal.dispatch(cid, bot.exchange.create_order)
        except Exception:
            if trade:
                mark_recovery(trade, "journal:" + cid)
                trade_model.commit()
            raise


def bind_journal(bot, order_id, trade_id):
    journal = getattr(bot, "order_journal", None)
    if journal is not None:
        journal.bind(order_id, trade_id)
