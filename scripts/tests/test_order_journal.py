"""P0.3 offline crash/recovery tests. No full Freqtrade runtime or network required."""

import json
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from freqtrade.order_journal import JournalBlocked, OrderJournal
from freqtrade.journal_execution import bind_journal, journal_create_order, reconcile_journal
from test_order_recovery import load_methods


PAIR = "BTC/USDT:USDT"
IDENTITY = {"account_id": "offline", "phase_id": "test", "database": "offline.sqlite"}
REQUEST = dict(pair=PAIR, side="buy", amount=2.0, rate=100.0, leverage=1.0,
               ordertype="limit", reduceOnly=False, initial_order=True, time_in_force="GTC")


def order(cid, status="open", filled=0.0):
    return dict(id="exchange-1", clientOrderId=cid, symbol=PAIR, side="buy", amount=2.0,
                filled=filled, remaining=0.0 if status == "closed" else 2.0-filled,
                status=status, average=100.0 if filled else None, price=100.0)


def snapshot(o=None, positions=None):
    return dict(complete=True, open_orders=[o] if o and o["status"] == "open" else [],
                positions=positions or [], orders={o["clientOrderId"]: o} if o else {},
                fills={o["clientOrderId"]: o["filled"]} if o else {})


def local(o):
    return {o["id"]: {**o, "trade_id": 7}}


class JournalTests(unittest.TestCase):
    def setUp(self):
        network = patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.j = OrderJournal(self.tmp.name, IDENTITY)
        self.addCleanup(lambda: self.j.close())
        self.assertEqual(self.j.reconcile(snapshot(), {}, {}), [])

    def restart(self):
        self.j.close()
        self.j = OrderJournal(self.tmp.name, IDENTITY)

    def prepare(self, **kwargs):
        return self.j.prepare({**REQUEST, **kwargs})

    def test_persist_before_send_and_stable_id(self):
        cid = self.prepare()
        def send(**kwargs):
            self.assertEqual(kwargs["client_order_id"], cid)
            self.assertEqual(self.j.intents()[0]["state"], "sending")
            return order(cid)
        self.j.dispatch(cid, send)
        self.assertEqual(self.j.intents()[0]["state"], "accepted")
        self.assertLessEqual(len(cid), 36)

    def test_timeout_and_lost_response_remain_uncertain(self):
        cid = self.prepare()
        send = Mock(side_effect=TimeoutError("accepted remotely, response lost"))
        with self.assertRaises(TimeoutError):
            self.j.dispatch(cid, send)
        self.assertEqual(self.j.intents()[0]["state"], "uncertain")
        with self.assertRaises(JournalBlocked):
            self.j.dispatch(cid, send)
        with self.assertRaises(JournalBlocked):
            self.prepare()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.j.intents()[0]["client_id"], cid)

    def test_crash_before_send(self):
        cid = self.prepare()
        self.restart()
        self.assertFalse(self.j.ready)
        self.assertTrue(self.j.reconcile(snapshot(), {}, {}))
        self.assertEqual(self.j.intents()[0]["client_id"], cid)
        self.assertEqual(self.j.intents()[0]["state"], "prepared")

    def test_crash_after_send_before_ack_save(self):
        cid = self.prepare()
        with self.assertRaises(KeyboardInterrupt):
            self.j.dispatch(cid, Mock(side_effect=KeyboardInterrupt))
        self.restart()
        self.assertEqual(self.j.intents()[0]["state"], "sending")
        self.assertTrue(self.j.reconcile(snapshot(order(cid)), {}, {}))
        self.assertEqual(self.j.intents()[0]["exchange_id"], "exchange-1")

    def test_crash_after_accept_before_trade_commit(self):
        cid = self.prepare()
        self.j.dispatch(cid, Mock(return_value=order(cid)))
        self.restart()
        self.assertTrue(self.j.reconcile(snapshot(order(cid)), {}, {}))
        self.assertFalse(self.j.intents()[0]["linked"])

    def test_crash_after_trade_commit_before_bind_recovers_reference(self):
        cid = self.prepare()
        o = order(cid)
        self.j.dispatch(cid, Mock(return_value=o))
        self.restart()
        self.assertEqual(self.j.reconcile(snapshot(o), local(o), {}), [])
        self.assertTrue(self.j.ready)
        self.assertEqual(self.j.intents()[0]["trade_id"], 7)

    def test_certain_rejection_requires_read_evidence(self):
        cid = self.prepare()
        o = order(cid, "rejected")
        self.j.dispatch(cid, Mock(return_value=o))
        self.assertFalse(self.j.ready)
        self.assertEqual(self.j.reconcile(snapshot(o), {}, {}), [])
        self.assertEqual(self.j.intents(), [])
        self.assertEqual(self.j.intents(False)[0]["state"], "rejected")
        self.prepare()  # New intent is now permitted.

    def test_negative_exception_is_not_certain_rejection(self):
        cid = self.prepare()
        with self.assertRaises(ValueError):
            self.j.dispatch(cid, Mock(side_effect=ValueError("duplicate orderLinkId")))
        self.assertTrue(self.j.reconcile(snapshot(), {}, {}))
        self.assertFalse(self.j.ready)

    def test_closed_order_found_in_history_and_position_verified(self):
        cid = self.prepare()
        o = order(cid, "closed", 2.0)
        s = snapshot(o, [dict(symbol=PAIR, side="long", amount=2.0)])
        self.assertEqual(s["open_orders"], [])
        self.assertEqual(self.j.reconcile(s, local(o), {(PAIR, "long"): 2.0}), [])
        self.assertEqual(self.j.intents(False)[0]["state"], "filled")
        self.assertEqual(self.j.intents(), [])

    def test_filled_without_local_trade_stays_blocked(self):
        cid = self.prepare()
        o = order(cid, "closed", 2.0)
        s = snapshot(o, [dict(symbol=PAIR, side="long", amount=2.0)])
        self.assertTrue(self.j.reconcile(s, {}, {}))
        self.assertFalse(self.j.ready)
        self.assertFalse(self.j.intents()[0]["settled"])

    def test_partial_fill_requires_local_quantity_agreement(self):
        cid = self.prepare()
        o = order(cid, filled=1.0)
        s = snapshot(o, [dict(symbol=PAIR, side="long", amount=1.0)])
        self.assertTrue(self.j.reconcile(s, local(o), {}))
        self.assertEqual(self.j.reconcile(s, local(o), {(PAIR, "long"): 1.0}), [])
        self.assertEqual(self.j.intents()[0]["state"], "partially_filled")
        self.assertFalse(self.j.intents()[0]["settled"])

    def test_partial_cancel_is_not_empty(self):
        cid = self.prepare()
        o = order(cid, "canceled", 1.0)
        self.assertTrue(self.j.reconcile(snapshot(o), {}, {}))
        self.assertFalse(self.j.intents()[0]["settled"])

    def test_inconclusive_does_not_unlock(self):
        cid = self.prepare()
        for s in ({"complete": False}, snapshot(), snapshot({**order(cid), "filled": None})):
            with self.subTest(s=s):
                self.assertTrue(self.j.reconcile(s, {}, {}))
                self.assertFalse(self.j.ready)

    def test_fill_endpoint_must_agree(self):
        cid = self.prepare()
        o = order(cid, "closed", 2.0)
        s = snapshot(o)
        s["fills"][cid] = 1.0
        self.assertTrue(self.j.reconcile(s, local(o), {}))
        self.assertFalse(self.j.intents()[0]["settled"])

    def test_origin_and_exchange_id_must_agree(self):
        cid = self.prepare()
        self.j.dispatch(cid, Mock(return_value=order(cid)))
        for update in ({"id": "other"}, {"clientOrderId": "external"}, {"side": "sell"}):
            o = {**order(cid), **update}
            s = snapshot(o)
            s["orders"] = {cid: o}
            self.assertTrue(self.j.reconcile(s, local(o), {}))

    def test_two_processes_cannot_own_shared_journal(self):
        code = ("import sys; from freqtrade.order_journal import OrderJournal, JournalBlocked; "
                "\ntry: OrderJournal(sys.argv[1], {})\nexcept JournalBlocked: sys.exit(23)")
        result = subprocess.run([sys.executable, "-B", "-c", code, self.tmp.name],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 23, result.stderr)

    def test_identity_change_refused(self):
        self.j.close()
        with self.assertRaises(JournalBlocked):
            OrderJournal(self.tmp.name, {**IDENTITY, "account_id": "different"})

    def test_abrupt_process_death_retains_sending_and_releases_lock(self):
        directory = str(Path(self.tmp.name) / "crashed")
        code = """
import json, os, sys
from freqtrade.order_journal import OrderJournal
j = OrderJournal(sys.argv[1], json.loads(sys.argv[2]))
j.ready = True
cid = j.prepare(json.loads(sys.argv[3]))
j.dispatch(cid, lambda **kwargs: os._exit(17))
"""
        result = subprocess.run([sys.executable, "-B", "-c", code, directory,
                                 json.dumps(IDENTITY), json.dumps(REQUEST)],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 17, result.stderr)
        recovered = OrderJournal(directory, IDENTITY)
        try:
            row = recovered.intents()[0]
            self.assertEqual(row["state"], "sending")
            send = Mock()
            with self.assertRaises(JournalBlocked):
                recovered.dispatch(row["client_id"], send)
            send.assert_not_called()
        finally:
            recovered.close()

    def test_external_order_latched_not_claimed_or_removed(self):
        o = order("foreign")
        s = snapshot()
        s["open_orders"] = [o]
        self.assertTrue(self.j.reconcile(s, {}, {}))
        self.assertEqual(self.j.intents(), [])
        self.assertTrue(self.j.reconcile(snapshot(), {}, {}))
        self.assertFalse(self.j.ready)

    def test_residual_position_blocks_until_quantities_match(self):
        s = snapshot(positions=[dict(symbol=PAIR, side="long", amount=1.0)])
        self.assertTrue(self.j.reconcile(s, {}, {(PAIR, "long"): 2.0}))
        self.assertEqual(self.j.reconcile(s, {}, {(PAIR, "long"): 1.0}), [])

    def test_unknown_position_remains_latched_after_disappearance(self):
        s = snapshot(positions=[dict(symbol=PAIR, side="short", amount=1.0)])
        self.assertTrue(self.j.reconcile(s, {}, {}))
        self.assertTrue(self.j.reconcile(snapshot(), {}, {}))
        self.assertFalse(self.j.ready)

    def test_corrupt_journal_refuses_startup(self):
        import sqlite3
        directory = Path(self.tmp.name) / "corrupt"
        directory.mkdir()
        (directory / "orders.sqlite").write_bytes(b"not a database")
        with self.assertRaises(sqlite3.DatabaseError):
            OrderJournal(directory, IDENTITY)

    def test_unknown_pair_does_not_block_unrelated_reduction(self):
        self.prepare()
        with self.assertRaises(JournalBlocked):
            self.prepare(reduceOnly=True, side="sell")
        cid = self.prepare(pair="ETH/USDT:USDT", reduceOnly=True, side="sell")
        self.assertEqual(len(self.j.intents()), 2)
        self.assertTrue(cid)

    def test_legacy_open_order_missing_blocks_until_p02_updates_it(self):
        o = order("legacy")
        self.assertTrue(self.j.reconcile(snapshot(), local(o), {}))
        o.update(status="canceled")
        self.assertEqual(self.j.reconcile(snapshot(), local(o), {}), [])

    def test_malformed_ack_is_unknown(self):
        cid = self.prepare()
        with self.assertRaises(JournalBlocked):
            self.j.dispatch(cid, Mock(return_value={"id": "exchange-1"}))
        self.assertEqual(self.j.intents()[0]["state"], "uncertain")

    def test_integration_send_failure_preserves_p02_marker(self):
        trade = SimpleNamespace(id=7, custom={})
        trade.get_custom_data = lambda key, default: trade.custom.get(key, default)
        trade.set_custom_data = lambda key, value: trade.custom.update({key: value})
        model = SimpleNamespace(get_trades_proxy=lambda: [], commit=Mock())
        exchange = SimpleNamespace(fetch_journal_snapshot=Mock(return_value=snapshot()),
                                   create_order=Mock(side_effect=TimeoutError))
        bot = SimpleNamespace(order_journal=self.j, exchange=exchange, config={})
        bot.has_unresolved_orders = lambda: not self.j.ready
        with self.assertRaises(TimeoutError):
            journal_create_order(bot, trade, model, **REQUEST)
        cid = self.j.intents()[0]["client_id"]
        self.assertIn("journal:" + cid, trade.custom["core_order_recovery_pending"])
        with self.assertRaises(JournalBlocked):
            journal_create_order(bot, trade, model, **REQUEST)
        self.assertEqual(exchange.create_order.call_count, 1)

    def test_integration_disabled_uses_existing_execution(self):
        exchange = SimpleNamespace(create_order=Mock(return_value={"id": "old"}))
        bot = SimpleNamespace(exchange=exchange, config={})
        self.assertEqual(journal_create_order(bot, None, None, **REQUEST), {"id": "old"})
        exchange.create_order.assert_called_once_with(**REQUEST)
        bot.config["prop_guard_enabled"] = True
        with self.assertRaises(JournalBlocked):
            journal_create_order(bot, None, None, **REQUEST)

    def test_stale_link_cannot_authorize_same_pair_exit(self):
        cid = self.prepare()
        o = order(cid)
        self.assertEqual(self.j.reconcile(snapshot(o), local(o), {}), [])
        self.assertTrue(self.j.intents()[0]["linked"])
        self.assertTrue(self.j.reconcile({"complete": False}, local(o), {}))
        with self.assertRaises(JournalBlocked):
            self.prepare(reduceOnly=True, side="sell")
        self.prepare(pair="ETH/USDT:USDT", reduceOnly=True, side="sell")

    def test_p02_lock_rechecked_at_dispatch_boundary(self):
        model = SimpleNamespace(get_trades_proxy=lambda: [], commit=Mock())
        exchange = SimpleNamespace(fetch_journal_snapshot=Mock(return_value=snapshot()), create_order=Mock())
        bot = SimpleNamespace(order_journal=self.j, exchange=exchange, config={},
                              has_unresolved_orders=lambda: True)
        with self.assertRaises(JournalBlocked):
            journal_create_order(bot, None, model, **REQUEST)
        self.assertEqual(self.j.intents(), [])
        exchange.create_order.assert_not_called()

    def test_actual_core_entry_gates_all_modes_on_journal(self):
        import logging
        Bot = load_methods("freqtrade/freqtradebot.py", "FreqtradeBot",
            {"execute_entry", "has_unresolved_orders"}, {"logger": logging.getLogger(__name__)})
        bot = Bot()
        bot.order_journal = self.j
        self.prepare()
        for mode in ("initial", "pos_adjust", "replace"):
            self.assertFalse(bot.execute_entry(PAIR, 100, mode=mode))

    def test_actual_core_force_exit_uses_journal_and_binds_after_commit(self):
        import logging
        from datetime import UTC, datetime
        from freqtrade.enums import ExitCheckTuple, ExitType, TradingMode
        from freqtrade.exceptions import InsufficientFundsError
        from freqtrade.order_recovery import pending_recovery
        trade = SimpleNamespace(id=7, pair=PAIR, amount=2.0, is_short=False, is_open=True,
            date_last_filled_utc=datetime.now(UTC), leverage=1.0, exit_side="sell", orders=[],
            has_open_orders=False, set_funding_fees=Mock(), get_custom_data=lambda key, default: default)
        model = SimpleNamespace(get_trades_proxy=lambda: [trade], commit=Mock())
        events = []
        model.commit.side_effect = lambda: events.append("commit")
        def bind(bot, oid, tid):
            self.assertEqual(events[-1], "commit")
            events.append("bind")
            bind_journal(bot, oid, tid)
        def parse(o, *args):
            events.append("parse")
            return SimpleNamespace(order_id=o["id"])
        ns = dict(Trade=model, Order=SimpleNamespace(parse_from_ccxt_object=parse),
            logger=logging.getLogger(__name__), pending_recovery=pending_recovery,
            journal_create_order=journal_create_order, bind_journal=bind,
            ExitType=ExitType, TradingMode=TradingMode, InsufficientFundsError=InsufficientFundsError,
            JournalBlocked=JournalBlocked,
            datetime=datetime, UTC=UTC, dt_now=lambda: datetime.now(UTC),
            strategy_safe_wrapper=lambda fn, **kwargs: fn)
        Bot = load_methods("freqtrade/freqtradebot.py", "FreqtradeBot", {"execute_trade_exit"}, ns)
        bot = Bot()
        bot.order_journal, bot.config = self.j, {}
        bot.trading_mode = TradingMode.FUTURES
        bot.exchange = SimpleNamespace(get_funding_fees=Mock(return_value=0),
            fetch_journal_snapshot=Mock(return_value=snapshot(positions=[
                dict(symbol=PAIR, side="long", amount=2.0)])),
            create_order=Mock(side_effect=lambda **kw: {**order(kw["client_order_id"]), "side": "sell"}))
        bot.strategy = SimpleNamespace(order_types={"exit": "market"}, order_time_in_force={"exit": "GTC"},
                                       confirm_trade_exit=lambda **kwargs: True)
        bot.get_valid_price = lambda custom, proposed: custom
        bot.cancel_stoploss_on_exchange = lambda trade, **kwargs: trade
        bot._safe_exit_amount = lambda trade, pair, amount: amount
        bot._exit_reason_cache, bot._notify_exit = {}, Mock()
        bot.exchange.fetch_journal_snapshot.return_value = snapshot(positions=[
            dict(symbol=PAIR, side="long", amount=3.0)])
        self.assertFalse(bot.execute_trade_exit(trade, 100, ExitCheckTuple(ExitType.FORCE_EXIT)))
        bot.exchange.create_order.assert_not_called()
        self.assertEqual(self.j.intents(), [])
        bot.exchange.fetch_journal_snapshot.return_value = snapshot(positions=[
            dict(symbol=PAIR, side="long", amount=2.0)])
        self.assertTrue(bot.execute_trade_exit(trade, 100, ExitCheckTuple(ExitType.FORCE_EXIT)))
        self.assertEqual(events[-3:], ["parse", "commit", "bind"])
        self.assertEqual(trade.orders[0].ft_order_tag, "force_exit")
        self.assertTrue(bot.exchange.create_order.call_args.kwargs["reduceOnly"])
        self.assertEqual(self.j.intents()[0]["trade_id"], 7)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        network = patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        import math
        self.Adapter = load_methods("freqtrade/exchange/bybit.py", "Bybit",
            {"_journal_pages", "_journal_market", "fetch_journal_snapshot"}, {"math": math})
        self.a = self.Adapter()

    @staticmethod
    def page(rows, cursor=""):
        return {"retCode": 0, "result": {"category": "linear", "list": rows, "nextPageCursor": cursor}}

    def test_pagination_all_pages(self):
        endpoint = Mock(side_effect=[self.page([1], "next"), self.page([2])])
        self.assertEqual(self.a._journal_pages(endpoint, {}), [1, 2])
        self.assertEqual(endpoint.call_args.args[0]["cursor"], "next")

    def test_bad_pagination_and_response_fail_closed(self):
        for pages in ([self.page([], "next"), self.page([], "next")],
                      [{"retCode": 1}], [{"retCode": 0, "result": {"list": []}}]):
            with self.subTest(pages=pages), self.assertRaises((ValueError, KeyError)):
                self.a._journal_pages(Mock(side_effect=pages), {})

    def setup_snapshot(self, status="Filled", qty="2"):
        import ccxt
        api = ccxt.bybit()
        market = {"id": "BTCUSDT", "symbol": PAIR, "base": "BTC", "quote": "USDT",
                  "settle": "USDT", "type": "swap", "swap": True, "spot": False,
                  "linear": True, "inverse": False, "contract": True, "option": False,
                  "future": False, "contractSize": 1.0,
                  "precision": {"amount": 0.001, "price": 0.1},
                  "limits": {"amount": {"min": 0.001, "max": 1000}}, "info": {}}
        api.set_markets([market])
        self.a._api = api
        self.a._contracts_to_amount = lambda pair, qty: qty
        self.a.amount_to_contract_precision = lambda pair, qty: qty
        self.a._order_contracts_to_amount = lambda o: o
        raw = dict(orderId="exchange-1", orderLinkId="ftj-test", symbol="BTCUSDT", side="Buy",
                   orderType="Limit", orderStatus=status, qty="2", cumExecQty=qty,
                   leavesQty=str(2-float(qty)), avgPrice="100", price="100", reduceOnly=False,
                   createdTime="1700000000000", updatedTime="1700000000001")
        api.privateGetV5OrderRealtime = Mock(return_value=self.page([]))
        api.privateGetV5OrderHistory = Mock(return_value=self.page([raw]))
        api.privateGetV5PositionList = Mock(return_value=self.page([]))
        executions = [] if float(qty) == 0 else [dict(orderId="exchange-1", orderLinkId="ftj-test",
            symbol="BTCUSDT", side="Buy", execId="fill1", execQty=qty)]
        api.privateGetV5ExecutionList = Mock(return_value=self.page(executions))
        return api, raw, [{"client_id": "ftj-test", "request": json.dumps(REQUEST)}]

    def test_actual_ccxt_client_id_maps_to_bybit_order_link_id(self):
        api, _, _ = self.setup_snapshot()
        request = api.create_order_request(PAIR, "limit", "buy", 2.0, 100.0,
                                           {"clientOrderId": "ftj-stable", "reduceOnly": False})
        self.assertEqual(request["orderLinkId"], "ftj-stable")

    def test_freqtrade_adapter_forwards_client_id_without_altering_params(self):
        Adapter = load_methods("freqtrade/exchange/exchange.py", "Exchange", {"create_order"}, {})
        adapter = Adapter()
        adapter._config = {"dry_run": False}
        adapter._get_params = Mock(return_value={"reduceOnly": True, "timeInForce": "IOC"})
        adapter.amount_to_precision = lambda pair, amount: amount
        adapter._amount_to_contracts = lambda pair, amount: amount
        adapter._order_needs_price = lambda side, kind: True
        adapter.price_to_precision = lambda pair, price: price
        adapter._order_contracts_to_amount = lambda value: value
        adapter._log_exchange_response = Mock()
        adapter._lev_prep = Mock()
        adapter._api = SimpleNamespace(create_order=Mock(return_value={**order("stable"), "type": "limit"}))
        adapter.create_order(**{**REQUEST, "reduceOnly": True, "time_in_force": "IOC"},
                             client_order_id="stable")
        params = adapter._api.create_order.call_args.args[-1]
        self.assertEqual(params, {"reduceOnly": True, "timeInForce": "IOC", "clientOrderId": "stable"})
        adapter._lev_prep.assert_not_called()

    def test_history_recovers_filled_order_not_in_open_orders(self):
        _, _, intents = self.setup_snapshot()
        s = self.a.fetch_journal_snapshot(intents)
        self.assertEqual(s["open_orders"], [])
        self.assertEqual(s["orders"]["ftj-test"]["status"], "closed")
        self.assertEqual(s["fills"]["ftj-test"], 2.0)

    def test_no_match_is_not_negative_execution_proof(self):
        api, _, intents = self.setup_snapshot()
        api.privateGetV5OrderHistory.return_value = self.page([])
        s = self.a.fetch_journal_snapshot(intents)
        self.assertEqual(s["orders"], {})

    def test_realtime_history_disagreement(self):
        api, raw, intents = self.setup_snapshot()
        api.privateGetV5OrderRealtime.side_effect = [self.page([]), self.page([{**raw, "cumExecQty": "1"}])]
        with self.assertRaises(ValueError):
            self.a.fetch_journal_snapshot(intents)

    def test_execution_wrong_origin_rejected(self):
        api, _, intents = self.setup_snapshot()
        api.privateGetV5ExecutionList.return_value["result"]["list"][0]["orderLinkId"] = "foreign"
        with self.assertRaises(ValueError):
            self.a.fetch_journal_snapshot(intents)


if __name__ == "__main__":
    unittest.main()
