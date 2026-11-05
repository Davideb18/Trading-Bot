"""Offline B1/V7 tests executing actual core methods with mocked infrastructure.

AST extraction avoids importing the bot's optional runtime dependencies. No method
under test is reimplemented: persistence/notifications/exchange are test doubles.
"""

import ast
import json
import logging
import math
import socket
import sqlite3
import sys
import traceback
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from freqtrade import constants
from freqtrade.enums import ExitCheckTuple, ExitType, State
from freqtrade.exceptions import DependencyException, ExchangeError, InvalidOrderException
from freqtrade.order_recovery import (
    RECOVERY_KEY, UNKNOWN_FORCE_EXIT, clear_recovery, mark_recovery,
    order_evidence_valid, pending_recovery,
)


class RPCException(Exception):
    pass


class Trade:
    rows = {}
    db = None
    session = SimpleNamespace(get=lambda model, key: Trade.rows.get(key))

    def __init__(self, base=10, side="buy"):
        self.id = len(self.rows) + 1
        self.rows[self.id] = self
        self.pair = "BTC/USDT:USDT"
        self.entry_side = side
        self.exit_side = "sell" if side == "buy" else "buy"
        self.is_short = side == "sell"
        self.is_open = True
        self.amount = base
        self.amount_requested = base
        self.open_rate = 100.0
        self.stake_amount = base * 100
        self.leverage = 1
        self.stop_loss_pct = -.99
        self.exit_reason = None
        self.orders = []
        if base:
            self.orders.append(Order(self, "base", filled=base, amount=base, status="closed"))

    @property
    def open_orders(self):
        return [o for o in self.orders if o.ft_is_open]

    @property
    def has_open_orders(self):
        return bool(self.open_orders)

    @property
    def nr_of_successful_entries(self):
        return sum(o.side == self.entry_side and o.filled > 0 and not o.ft_is_open for o in self.orders)

    def set_custom_data(self, *, key, value):
        self.db.execute("INSERT OR REPLACE INTO data VALUES (?, ?, ?)", (self.id, key, json.dumps(value)))
        self.db.commit()

    def get_custom_data(self, *, key, default=None):
        row = self.db.execute("SELECT value FROM data WHERE trade_id=? AND key=?", (self.id, key)).fetchone()
        return json.loads(row[0]) if row else default

    def update_order(self, snapshot):
        o = self.select_order_by_order_id(snapshot["id"])
        o.status, o.filled = snapshot["status"], snapshot["filled"]
        o.remaining = snapshot["remaining"]
        o.ft_is_open = o.status not in constants.NON_OPEN_EXCHANGE_STATES

    def update_trade(self, order, *args):
        self.amount = sum((1 if o.side == self.entry_side else -1) * o.filled
                          for o in self.orders if not o.ft_is_open)
        if order.side == self.exit_side and self.amount <= 0:
            self.is_open = False

    def select_order_by_order_id(self, order_id):
        return next((o for o in self.orders if o.order_id == order_id), None)

    def delete(self):
        self.rows.pop(self.id, None)
        self.db.execute("DELETE FROM data WHERE trade_id=?", (self.id,))
        self.db.commit()

    @classmethod
    def get_open_trades(cls):
        return [t for t in cls.rows.values() if t.is_open]

    @classmethod
    def commit(cls):
        cls.db.commit()


class Order:
    def __init__(self, trade, order_id="dca", side=None, filled=0.0, amount=2.0, status="open", tag=None):
        self.trade = trade
        self.order_id = order_id
        self.ft_pair = trade.pair
        self.side = side or trade.entry_side
        self.ft_order_side = self.side
        self.ft_order_tag = tag
        self.ft_cancel_reason = None
        self.filled, self.amount = filled, amount
        self.remaining = amount - filled
        self.status = status
        self.ft_is_open = status not in constants.NON_OPEN_EXCHANGE_STATES
        self.order_date_utc = datetime.now(UTC)
        self.price = 100
        self.ft_amount = amount

    safe_remaining = property(lambda self: self.remaining)
    safe_price = property(lambda self: self.price)

    @classmethod
    def get_open_orders(cls):
        return [o for t in Trade.rows.values() for o in t.orders if o.ft_is_open]


def snapshot(order, status="open", filled=0.0):
    return {"id": order.order_id, "symbol": order.ft_pair, "side": order.side,
            "status": status, "type": "limit", "amount": order.amount, "filled": filled,
            "remaining": 0.0 if status == "closed" else order.amount - filled,
            "price": 100.0, "average": 100.0 if filled else None, "fee": {}}


def load_methods(path, class_name, method_names, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    cls.bases = []
    cls.decorator_list = []
    cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in method_names]
    code = "from __future__ import annotations\n" + ast.unparse(cls)
    exec(compile(code, path, "exec"), namespace)
    return namespace[class_name]


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)
        Trade.rows = {}
        Trade.db = sqlite3.connect(":memory:")
        Trade.db.execute("CREATE TABLE data (trade_id INT, key TEXT, value TEXT, PRIMARY KEY(trade_id,key))")
        self.addCleanup(Trade.db.close)
        ns = dict(Trade=Trade, Order=Order, constants=constants, datetime=datetime, UTC=UTC,
                  timedelta=timedelta, logger=logging.getLogger("offline_recovery"),
                  traceback=traceback, ExchangeError=ExchangeError, InvalidOrderException=InvalidOrderException,
                  DependencyException=DependencyException, RPCException=RPCException,
                  ExitType=ExitType, ExitCheckTuple=ExitCheckTuple, State=State,
                  CANCEL_REASON=constants.CANCEL_REASON, isclose=math.isclose,
                  mark_recovery=mark_recovery, clear_recovery=clear_recovery,
                  pending_recovery=pending_recovery, order_evidence_valid=order_evidence_valid,
                  UNKNOWN_FORCE_EXIT=UNKNOWN_FORCE_EXIT,
                  safe_value_fallback2=lambda first, second, k1, k2: first.get(k1, second.get(k2)),
                  sleep=lambda _: None)
        self.Bot = load_methods("freqtrade/freqtradebot.py", "FreqtradeBot", {
            "startup_update_open_orders", "has_unresolved_orders", "manage_open_orders",
            "handle_cancel_enter", "handle_cancel_exit", "handle_cancel_order",
            "handle_replace_order", "update_trade_state", "order_obj_or_raise",
            "execute_entry", "execute_trade_exit",
        }, ns)
        self.Rpc = load_methods("freqtrade/rpc/rpc.py", "RPC", {"__exec_force_exit", "_rpc_force_exit"}, ns)
        self.bot = self.Bot()
        self.bot.config = {"dry_run": False, "exchange": {}, "unfilledtimeout": {}}
        self.bot.state = State.RUNNING
        self.bot._exit_lock = Lock()
        self.bot.exchange = Mock()
        self.bot.exchange.check_order_canceled_empty.side_effect = (
            lambda o: o["status"] in constants.CANCELED_EXCHANGE_STATES and o["filled"] == 0
        )
        self.bot.exchange.get_rate.return_value = 100
        self.bot.exchange.get_min_pair_stake_amount.return_value = 0
        self.bot.wallets = Mock()
        self.bot.strategy = SimpleNamespace(order_types={"entry": "limit", "exit": "limit", "force_exit": "market"},
                                            stoploss=-.99, ft_check_timed_out=lambda *args: False)
        self.bot.handle_order_fee = Mock()
        self.bot._update_trade_after_fill = lambda trade, *args: trade
        self.bot.order_close_notify = Mock()
        self.bot._notify_enter_cancel = Mock()
        self.bot._notify_exit_cancel = Mock()
        self.bot.replace_order = Mock()
        self.bot.replace_order_failed = Mock()
        self.bot.execute_trade_exit = Mock(return_value=True)
        self.rpc = self.Rpc()
        self.rpc._freqtrade = self.bot

    def trade_order(self, base=10, **kwargs):
        trade = Trade(base)
        order = Order(trade, **kwargs)
        trade.orders.append(order)
        return trade, order

    def force(self, trade):
        return self.rpc._RPC__exec_force_exit(trade, "market")

    def test_first_entry_cancelled_no_position_no_exit(self):
        trade, order = self.trade_order(base=0)
        self.bot.exchange.fetch_order.side_effect = [snapshot(order), snapshot(order, "canceled")]
        self.assertTrue(self.force(trade))
        self.assertNotIn(trade.id, Trade.rows)
        self.bot.execute_trade_exit.assert_not_called()

    def test_pending_dca_cancelled_existing_position_is_closed(self):
        trade, order = self.trade_order()
        self.bot.exchange.fetch_order.side_effect = [snapshot(order), snapshot(order, "canceled")]
        self.assertTrue(self.force(trade))
        self.bot.execute_trade_exit.assert_called_once()
        self.assertEqual(trade.amount, 10)
        self.assertFalse(pending_recovery(trade))
        self.bot.exchange.cancel_order_with_result.assert_not_called()

    def test_partial_dca_uses_reconciled_residual_quantity(self):
        trade, order = self.trade_order()
        self.bot.exchange.fetch_order.side_effect = [snapshot(order, filled=1), snapshot(order, "canceled", 1)]
        self.assertTrue(self.force(trade))
        self.assertEqual(self.bot.execute_trade_exit.call_args.args[0].amount, 11)

    def test_dca_fills_during_cancel(self):
        trade, order = self.trade_order()
        self.bot.exchange.fetch_order.side_effect = [snapshot(order), snapshot(order, "closed", 2)]
        self.assertTrue(self.force(trade))
        self.assertEqual(trade.amount, 12)

    def test_cancel_timeout_reconciled_by_lookup_can_exit(self):
        trade, order = self.trade_order()
        self.bot.exchange.cancel_order.side_effect = ExchangeError("timeout")
        self.bot.exchange.fetch_order.side_effect = [snapshot(order), snapshot(order, "canceled")]
        self.assertTrue(self.force(trade))
        self.bot.execute_trade_exit.assert_called_once()

    def test_uncertain_cancel_never_uses_synthetic_fallback(self):
        trade, order = self.trade_order()
        self.bot.exchange.cancel_order.side_effect = ExchangeError("timeout")
        self.bot.exchange.fetch_order.side_effect = [snapshot(order), InvalidOrderException("missing")]
        self.assertFalse(self.force(trade))
        self.assertTrue(order.ft_is_open)
        self.assertIn(order.order_id, pending_recovery(trade))
        self.bot.exchange.cancel_order_with_result.assert_not_called()
        self.bot.execute_trade_exit.assert_not_called()

    def test_pending_cancel_then_successful_retry(self):
        trade, order = self.trade_order()
        self.bot.exchange.fetch_order.side_effect = [snapshot(order), snapshot(order)]
        self.assertFalse(self.force(trade))
        self.bot.exchange.fetch_order.side_effect = [snapshot(order, "canceled")]
        self.assertTrue(self.force(trade))
        self.bot.execute_trade_exit.assert_called_once()

    def test_retry_of_known_open_force_exit_does_not_duplicate(self):
        trade, order = self.trade_order(side="sell", order_id="exit", tag="force_exit")
        self.bot.exchange.fetch_order.return_value = snapshot(order)
        self.assertTrue(self.force(trade))
        self.assertTrue(self.force(trade))
        self.bot.execute_trade_exit.assert_not_called()
        self.bot.exchange.cancel_order.assert_not_called()

    def test_existing_exit_does_not_hide_uncertain_entry(self):
        trade, exit_order = self.trade_order(side="sell", order_id="exit", tag="force_exit")
        dca = Order(trade)
        trade.orders.append(dca)
        self.bot.exchange.fetch_order.side_effect = [snapshot(exit_order), ExchangeError("missing DCA")]
        self.assertFalse(self.force(trade))
        self.assertIn(dca.order_id, pending_recovery(trade))
        self.bot.execute_trade_exit.assert_not_called()

    def test_already_filled_exit_does_not_send_again(self):
        trade, order = self.trade_order(side="sell", amount=10)
        self.bot.exchange.fetch_order.return_value = snapshot(order, "closed", 10)
        self.assertTrue(self.force(trade))
        self.assertFalse(trade.is_open)
        self.bot.execute_trade_exit.assert_not_called()

    def test_partial_exit_cancel_then_close_remaining(self):
        trade, order = self.trade_order(side="sell", amount=10)
        self.bot.exchange.fetch_order.side_effect = [snapshot(order, filled=3), snapshot(order, "canceled", 3)]
        self.assertTrue(self.force(trade))
        self.assertEqual(trade.amount, 7)

    def test_unknown_exit_submission_latches_across_rpc_instances(self):
        trade = Trade()
        self.bot.execute_trade_exit.side_effect = ExchangeError("response lost")
        self.assertFalse(self.force(trade))
        other = self.Rpc()
        other._freqtrade = self.bot
        self.assertFalse(other._RPC__exec_force_exit(trade, "market"))
        self.bot.execute_trade_exit.assert_called_once()
        self.assertIn(UNKNOWN_FORCE_EXIT, pending_recovery(trade))
        self.assertTrue(self.bot.has_unresolved_orders())
        self.assertFalse(self.Bot.execute_trade_exit(self.bot, trade, 100, ExitCheckTuple(ExitType.FORCE_EXIT)))

    def test_malformed_submission_response_also_prevents_resubmit(self):
        trade = Trade()
        self.bot.execute_trade_exit.side_effect = KeyError("missing order id")
        self.assertFalse(self.force(trade))
        self.assertFalse(self.force(trade))
        self.bot.execute_trade_exit.assert_called_once()
        self.assertIn(UNKNOWN_FORCE_EXIT, pending_recovery(trade))

    def test_all_reports_unresolved_instead_of_success(self):
        trade, order = self.trade_order()
        self.bot.exchange.fetch_order.side_effect = ExchangeError("timeout")
        with self.assertRaisesRegex(RPCException, "Unresolved force-exit"):
            self.rpc._rpc_force_exit("all")

    def test_recent_and_old_missing_orders_preserved_across_recovery(self):
        for age in (0, 6, 60):
            with self.subTest(age=age):
                trade, order = self.trade_order()
                order.order_date_utc -= timedelta(days=age)
                self.bot.exchange.fetch_order_or_stoploss_order.side_effect = InvalidOrderException("not yet visible")
                self.bot.startup_update_open_orders()
                self.bot.startup_update_open_orders()
                self.assertIn(trade.id, Trade.rows)
                self.assertTrue(order.ft_is_open)
                self.assertIn(order.order_id, pending_recovery(trade))
                self.assertTrue(self.bot.has_unresolved_orders())
                self.bot.exchange.cancel_order.assert_not_called()

    def test_confirmed_cancelled_first_entry_is_removed(self):
        trade, order = self.trade_order(base=0)
        self.bot.exchange.fetch_order_or_stoploss_order.return_value = snapshot(order, "canceled")
        self.bot.startup_update_open_orders()
        self.assertNotIn(trade.id, Trade.rows)
        self.assertFalse(self.bot.has_unresolved_orders())

    def test_missing_filled_order_later_reconciles_without_resubmit(self):
        trade, order = self.trade_order(base=0)
        self.bot.exchange.fetch_order_or_stoploss_order.side_effect = InvalidOrderException("delayed")
        self.bot.startup_update_open_orders()
        self.bot.exchange.fetch_order_or_stoploss_order.side_effect = None
        self.bot.exchange.fetch_order_or_stoploss_order.return_value = snapshot(order, "closed", 2)
        self.bot.startup_update_open_orders()
        self.assertEqual(trade.amount, 2)
        self.assertFalse(self.bot.has_unresolved_orders())
        self.bot.exchange.create_order.assert_not_called()

    def test_partial_fill_stays_open_and_preserves_reference(self):
        trade, order = self.trade_order()
        self.bot.exchange.fetch_order_or_stoploss_order.return_value = snapshot(order, filled=1)
        self.bot.startup_update_open_orders()
        self.assertTrue(order.ft_is_open)
        self.assertEqual(order.filled, 1)
        self.assertFalse(self.bot.has_unresolved_orders())

    def test_partial_cancelled_dca_recovery_preserves_position(self):
        trade, order = self.trade_order()
        self.bot.exchange.fetch_order_or_stoploss_order.return_value = snapshot(order, "canceled", 1)
        self.bot.startup_update_open_orders()
        self.assertFalse(order.ft_is_open)
        self.assertEqual(trade.amount, 11)
        self.assertIn(trade.id, Trade.rows)

    def test_orphan_order_blocks_entries_without_deleting_record(self):
        trade, order = self.trade_order()
        order.trade = None
        self.bot.startup_update_open_orders()
        self.assertTrue(self.bot.has_unresolved_orders())
        self.assertTrue(order.ft_is_open)
        self.bot.exchange.fetch_order_or_stoploss_order.assert_not_called()

    def test_timeout_and_inconclusive_response_remain_blocked(self):
        trade, order = self.trade_order()
        for response in (None, {}, snapshot(order) | {"filled": None},
                         snapshot(order) | {"status": "unknown"}, snapshot(order) | {"id": "wrong"}):
            self.bot.exchange.fetch_order_or_stoploss_order.return_value = response
            self.bot.startup_update_open_orders()
            self.assertTrue(self.bot.has_unresolved_orders())
            self.assertTrue(order.ft_is_open)
        self.bot.exchange.fetch_order_or_stoploss_order.side_effect = ExchangeError("timeout")
        self.bot.startup_update_open_orders()
        self.assertTrue(self.bot.has_unresolved_orders())

    def test_management_resolves_known_ids_and_keeps_other_markers(self):
        trade, order = self.trade_order()
        mark_recovery(trade, order.order_id)
        mark_recovery(trade, UNKNOWN_FORCE_EXIT)
        self.bot.exchange.fetch_order.return_value = snapshot(order)
        self.bot.manage_open_orders()
        self.assertEqual(pending_recovery(trade), [UNKNOWN_FORCE_EXIT])
        self.bot.exchange.fetch_order.side_effect = ExchangeError("offline")
        self.bot.manage_open_orders()
        self.assertIn(order.order_id, pending_recovery(trade))

    def test_entry_dca_replacement_gates_use_persistent_marker(self):
        trade, order = self.trade_order()
        mark_recovery(trade, order.order_id)
        new_bot = self.Bot()
        self.assertTrue(new_bot.has_unresolved_orders())
        for mode in ("initial", "pos_adjust", "replace"):
            self.assertFalse(self.Bot.execute_entry(self.bot, trade.pair, 100, trade=trade, mode=mode))
        self.bot.handle_replace_order(snapshot(order), order, trade, 101, True, "replace", replacing=True)
        self.bot.replace_order_failed.assert_not_called()
        self.bot.exchange.create_order.assert_not_called()

    def test_ordinary_replacement_keeps_cancel_boolean_semantics(self):
        trade, order = self.trade_order(base=0)
        self.bot.exchange.cancel_order_with_result.return_value = snapshot(order, "canceled")
        self.bot.execute_entry = Mock(return_value=True)
        self.bot.handle_replace_order(snapshot(order), order, trade, 101, True, "replace", replacing=True)
        self.bot.execute_entry.assert_called_once()
        self.assertEqual(self.bot.execute_entry.call_args.kwargs["mode"], "replace")
        self.assertIn(trade.id, Trade.rows)
        self.bot.replace_order_failed.assert_not_called()

    def test_regressing_fills_and_nonfinite_values_rejected(self):
        trade, order = self.trade_order(filled=1)
        self.assertFalse(order_evidence_valid(snapshot(order), order, trade.pair))
        for bad in (None, True, math.nan, math.inf, -1):
            self.assertFalse(order_evidence_valid(snapshot(order, filled=1) | {"filled": bad}, order, trade.pair))

    def test_short_position_force_exit_is_symmetric(self):
        trade = Trade(side="sell")
        order = Order(trade)
        trade.orders.append(order)
        self.bot.exchange.fetch_order.side_effect = [snapshot(order), snapshot(order, "canceled")]
        self.assertTrue(self.force(trade))
        self.assertEqual(trade.amount, 10)


if __name__ == "__main__":
    unittest.main()
