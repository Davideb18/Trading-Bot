"""Offline cancellation paths: real core methods, SQLAlchemy ORM and SQLite journal.

Only exchange responses, notifications and pricing/execution setup are doubles.
AST loading avoids importing unrelated bot runtime services, not the tested methods.
"""
import json
import logging
import math
import socket
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
from freqtrade import constants
from freqtrade.constants import CANCEL_REASON
from freqtrade.enums import TradingMode, ExitCheckTuple, ExitType
from freqtrade.exceptions import DependencyException, ExchangeError, InvalidOrderException
from freqtrade.persistence import Trade, Order, init_db
from freqtrade.order_recovery import (
    clear_recovery, mark_recovery, pending_recovery, order_evidence_valid, verified_prop_cancel,
    UNKNOWN_FORCE_EXIT,
)
from freqtrade.order_journal import OrderJournal, JournalBlocked
from freqtrade.journal_execution import (
    delete_verified_empty_trade, reconcile_journal, journal_create_order,
)
from freqtrade.emergency_authority import EmergencyTakeover, require_core_authority
from freqtrade.prop_controls import sync_prop_controls
from freqtrade.prop_profile import profile_from_config
from prop_risk_guard import reconciled_state
from test_prop_profile import configured_account, baseline
from test_order_recovery import load_methods

PAIR = 'BTC/USDT:USDT'


class CancellationTests(unittest.TestCase):
    def setUp(self):
        network = patch.object(socket.socket, 'connect', side_effect=AssertionError('Network forbidden'))
        network.start()
        self.addCleanup(network.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        init_db('sqlite://')
        engine = Trade.session.get_bind()
        self.addCleanup(engine.dispose)
        self.addCleanup(Trade.session.remove)
        config = configured_account()
        config.update(user_data_dir=self.root, unfilledtimeout={})
        profile = profile_from_config(config)
        now = datetime.now(UTC)
        state = reconciled_state(profile, baseline(profile, now=now), now, halted=False)
        state.update(data_valid=True, wake_up_lock=False, remote_identity_verified=True)
        (self.root / 'risk_manager_prop_state.json').write_text(json.dumps(state))
        identity = {k: profile[k] for k in ('profile_id', 'account_id', 'challenge_id', 'phase_id',
                                           'exchange', 'environment', 'currency')}
        self.j = OrderJournal(self.root / 'order_journal', identity)
        self.addCleanup(self.j.close)
        ns = dict(globals(), logger=logging.getLogger('offline_cancel'), isclose=math.isclose,
                  sleep=lambda _: None)
        methods = {'handle_cancel_enter', 'handle_cancel_exit', 'handle_cancel_order',
                   'handle_replace_order', 'replace_order_failed', 'manage_open_orders',
                   'update_trade_state', 'order_obj_or_raise', 'has_unresolved_orders'}
        Bot = load_methods('freqtrade/freqtradebot.py', 'FreqtradeBot', methods, ns)
        self.bot = Bot()
        self.bot.config = config
        self.bot.order_journal = self.j
        self.bot.exchange = Mock()
        self.bot.exchange.get_min_pair_stake_amount.return_value = 0
        self.bot.exchange.check_order_canceled_empty.side_effect = (
            lambda o: o['status'] in constants.CANCELED_EXCHANGE_STATES and o['filled'] == 0)
        self.bot.wallets = Mock()
        self.bot.strategy = SimpleNamespace(stoploss=-.99, order_types={'entry': 'limit', 'exit': 'limit'},
                                            ft_check_timed_out=lambda *a: False)
        self.bot.handle_order_fee = Mock()
        self.bot._update_trade_after_fill = lambda trade, *a: trade
        self.bot.order_close_notify = Mock()
        self.bot._notify_enter_cancel = Mock()
        self.bot._notify_exit_cancel = Mock()
        self.bot.replace_order = Mock()
        self.bot.execute_entry = Mock(return_value=False)
        self.trade = Trade(pair=PAIR, stake_amount=200, amount=0, open_rate=100,
                           fee_open=0, fee_close=0, exchange='bybit', is_open=True,
                           open_date=now, leverage=1, trading_mode=TradingMode.FUTURES)
        Trade.session.add(self.trade)
        Trade.commit()
        self.trade_id = self.trade.id
        self.request = dict(pair=PAIR, side='buy', amount=2., rate=100., leverage=1.,
                            ordertype='limit', reduceOnly=False, initial_order=True, time_in_force='GTC')
        self.j.ready = True
        self.cid = self.j.prepare(self.request, self.trade.id)
        self.remote = self.proof('open')
        self.j.dispatch(self.cid, lambda **kw: self.remote)
        self.order = Order.parse_from_ccxt_object(self.remote, PAIR, 'buy', 2., 100.)
        self.trade.orders.append(self.order)
        Trade.commit()
        self.bot.exchange.fetch_order.side_effect = lambda *a: dict(self.remote)
        self.bot.exchange.fetch_journal_snapshot.side_effect = lambda *a: self.snapshot()
        reconcile_journal(self.bot, Trade)

    def proof(self, status, filled=0):
        return dict(id='entry-1', clientOrderId=self.cid, symbol=PAIR, side='buy',
                    status=status, amount=2., filled=float(filled),
                    remaining=0. if status == 'closed' else 2.-filled,
                    type='limit', price=100., average=100. if filled else None,
                    fee={}, reduceOnly=False)

    def snapshot(self):
        from snapshot_fixtures import timed_snapshot
        p = self.remote
        return timed_snapshot(dict(complete=True, open_orders=[p] if p['status'] == 'open' else [],
                    positions=[dict(symbol=PAIR, side='long', amount=p['filled'], mark_price=100.)]
                    if p['filled'] else [], orders={self.cid: p}, fills={self.cid: p['filled']}))

    def exists(self):
        return Trade.session.get(Trade, self.trade_id) is not None

    def replace(self):
        self.bot.handle_replace_order(self.proof('open'), self.order, self.trade,
                                      101., True, 'replace', replacing=True)

    def cancel(self, replacing=False):
        return self.bot.handle_cancel_enter(self.trade, self.proof('open'), self.order,
                                            'timeout', replacing=replacing)

    def test_open_cancel_preserves_trade_and_blocks_repeated_replacement(self):
        self.replace()
        self.assertTrue(self.exists())
        self.assertTrue(self.order.ft_is_open)
        self.assertIn(self.order.order_id, pending_recovery(self.trade))
        self.replace()
        self.bot.exchange.cancel_order.assert_called_once()
        self.bot.execute_entry.assert_not_called()

    def test_synthetic_cancel_helper_is_never_used(self):
        self.bot.exchange.cancel_order_with_result.return_value = self.proof('canceled')
        self.bot.exchange.cancel_order.side_effect = InvalidOrderException('unknown')
        self.bot.exchange.fetch_order.side_effect = [self.proof('open'), InvalidOrderException('unknown')]
        self.assertFalse(self.cancel())
        self.assertTrue(self.exists())
        self.assertTrue(self.order.ft_is_open)
        self.bot.exchange.cancel_order_with_result.assert_not_called()

    def test_timeout_remains_unknown(self):
        self.bot.exchange.cancel_order.side_effect = ExchangeError('timeout')
        self.bot.exchange.fetch_order.side_effect = [self.proof('open'), ExchangeError('timeout')]
        self.assertFalse(self.cancel())
        self.assertTrue(self.exists())
        self.assertTrue(pending_recovery(self.trade))

    def test_timeout_then_terminal_lookup_allows_cleanup(self):
        self.bot.exchange.cancel_order.side_effect = ExchangeError('timeout')
        self.remote = self.proof('canceled')
        self.bot.exchange.fetch_order.side_effect = [self.proof('open'), self.remote]
        self.assertTrue(self.cancel())
        self.assertFalse(self.exists())
        self.assertEqual(self.j.intents(), [])

    def test_partial_fill_during_cancel_is_applied_and_preserved(self):
        self.remote = self.proof('canceled', 1)
        self.assertFalse(self.cancel())
        self.assertTrue(self.exists())
        self.assertEqual(self.trade.amount, 1)
        self.assertEqual(self.order.filled, 1)
        self.assertFalse(pending_recovery(self.trade))

    def test_complete_fill_is_not_empty_cancellation(self):
        self.remote = self.proof('closed', 2)
        self.assertFalse(self.cancel())
        self.assertEqual(self.trade.amount, 2)
        self.assertTrue(self.exists())

    def test_p06_denial_after_verified_cancel_cleans_empty_trade(self):
        self.remote = self.proof('canceled')
        self.bot.execute_entry.side_effect = lambda **kw: journal_create_order(
            self.bot, self.trade, Trade, **self.request)
        self.replace()
        self.assertFalse(self.exists())
        self.assertTrue(any(i['state'] == 'risk_denied' for i in self.j.intents(active=False)))
        self.bot.exchange.create_order.assert_not_called()

    def test_uncertain_replacement_retains_orm_markers_and_journal(self):
        self.remote = self.proof('canceled')
        self.bot.execute_entry.side_effect = lambda **kw: journal_create_order(
            self.bot, self.trade, Trade, **self.request)
        self.bot.exchange.create_order.side_effect = ExchangeError('accepted but response lost')
        with patch('freqtrade.journal_execution.require_risk_budget', return_value={'authorized': True}):
            self.replace()
        self.assertTrue(self.exists())
        uncertain = [i for i in self.j.intents() if i['state'] == 'uncertain']
        self.assertEqual(len(uncertain), 1)
        self.assertIn('journal:' + uncertain[0]['client_id'], pending_recovery(self.trade))
        self.assertEqual(len(self.trade.orders), 1)
        self.replace()
        self.bot.exchange.create_order.assert_called_once()

    def test_deferred_cleanup_retries_after_reconciliation_recovers(self):
        self.remote = self.proof('canceled')
        self.bot.exchange.fetch_journal_snapshot.side_effect = None
        self.bot.exchange.fetch_journal_snapshot.return_value = {'complete': False}
        self.assertTrue(self.cancel())
        self.assertTrue(self.exists())
        self.bot.exchange.fetch_journal_snapshot.side_effect = lambda *a: self.snapshot()
        self.bot.manage_open_orders()
        self.assertFalse(self.exists())

    def test_unknown_force_exit_marker_prevents_cleanup(self):
        mark_recovery(self.trade, 'force_exit_submission_unknown')
        self.remote = self.proof('canceled')
        self.cancel()
        self.assertTrue(self.exists())
        self.assertIn('force_exit_submission_unknown', pending_recovery(self.trade))

    def test_handoff_prevents_cancellation_and_cleanup(self):
        with self.j.db:
            self.j.db.execute('INSERT INTO emergency VALUES (1,?)', (json.dumps({'handoff': True}),))
        with self.assertRaises(EmergencyTakeover):
            self.cancel()
        self.bot.exchange.cancel_order.assert_not_called()
        self.assertTrue(self.exists())

    def test_exit_unknown_preserves_open_order(self):
        self.order.side = 'sell'
        self.order.ft_order_side = 'sell'
        self.remote['side'] = 'sell'
        Trade.commit()
        result = self.bot.handle_cancel_exit(self.trade, self.remote, self.order, 'replace')
        self.assertFalse(result)
        self.assertTrue(self.order.ft_is_open)
        self.assertTrue(pending_recovery(self.trade))
        self.bot.exchange.cancel_order_with_result.assert_not_called()

    def test_terminal_input_is_rechecked_not_trusted(self):
        self.bot.exchange.fetch_order.side_effect = InvalidOrderException('unverified')
        self.assertFalse(self.bot.handle_cancel_enter(
            self.trade, self.proof('canceled'), self.order, 'timeout'))
        self.assertTrue(self.exists())

    def test_partial_exit_cancel_preserves_remaining_position(self):
        self.remote = self.proof('closed', 2)
        self.bot.update_trade_state(self.trade, self.order.order_id, self.remote)
        exit_open = {**self.proof('open'), 'id': 'exit-1', 'side': 'sell', 'reduceOnly': True}
        exit_order = Order.parse_from_ccxt_object(exit_open, PAIR, 'sell', 2., 100.)
        self.trade.orders.append(exit_order)
        Trade.commit()
        terminal = {**exit_open, 'status': 'canceled', 'filled': 1., 'remaining': 1., 'average': 100.}
        self.bot.exchange.fetch_order.side_effect = [exit_open, terminal]
        self.assertTrue(self.bot.handle_cancel_exit(self.trade, exit_open, exit_order, 'replace'))
        self.assertEqual(exit_order.filled, 1.)
        self.assertFalse(exit_order.ft_is_open)
        self.assertEqual(self.trade.amount, 1.)
        self.assertTrue(self.exists())
        self.assertFalse(pending_recovery(self.trade))

    def test_cancel_ack_cannot_override_open_lookup(self):
        self.bot.exchange.cancel_order.return_value = self.proof('canceled')
        self.assertFalse(self.cancel())
        self.assertTrue(self.order.ft_is_open)
        self.assertTrue(pending_recovery(self.trade))

    def test_force_exit_empty_entry_retains_verified_cleanup(self):
        Rpc = load_methods('freqtrade/rpc/rpc.py', 'RPC', {'__exec_force_exit'},
                           dict(globals(), logger=logging.getLogger('offline_force')))
        rpc = Rpc()
        rpc._freqtrade = self.bot
        self.bot.execute_trade_exit = Mock()
        self.remote = self.proof('canceled')
        self.assertTrue(rpc._RPC__exec_force_exit(self.trade, 'market'))
        self.assertFalse(self.exists())
        self.bot.execute_trade_exit.assert_not_called()


class PartialFillReconcileAndExitTests(unittest.TestCase):
    """ORM + SQLite regression tests for open partial fills and pre-exit cancel reconciliation."""

    def setUp(self):
        from freqtrade.exceptions import InsufficientFundsError
        from freqtrade.journal_execution import bind_journal
        from freqtrade.strategy.strategy_wrapper import strategy_safe_wrapper
        from freqtrade.util import dt_now

        network = patch.object(socket.socket, 'connect', side_effect=AssertionError('Network forbidden'))
        network.start()
        self.addCleanup(network.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        init_db('sqlite://')
        engine = Trade.session.get_bind()
        self.addCleanup(engine.dispose)
        self.addCleanup(Trade.session.remove)
        config = configured_account()
        config.update(user_data_dir=self.root, unfilledtimeout={}, prop_guard_enabled=True)
        profile = profile_from_config(config)
        now = datetime.now(UTC)
        state = reconciled_state(profile, baseline(profile, now=now), now, halted=False)
        state.update(data_valid=True, wake_up_lock=False, remote_identity_verified=True)
        (self.root / 'risk_manager_prop_state.json').write_text(json.dumps(state))
        identity = {k: profile[k] for k in ('profile_id', 'account_id', 'challenge_id', 'phase_id',
                                           'exchange', 'environment', 'currency')}
        self.j = OrderJournal(self.root / 'order_journal', identity)
        self.addCleanup(self.j.close)
        ns = dict(
            globals(),
            logger=logging.getLogger('offline_partial_exit'),
            isclose=math.isclose,
            sleep=lambda _: None,
            InsufficientFundsError=InsufficientFundsError,
            bind_journal=bind_journal,
            strategy_safe_wrapper=strategy_safe_wrapper,
            dt_now=dt_now,
        )
        methods = {
            'execute_trade_exit', 'handle_similar_open_order', 'cancel_open_orders_of_trade',
            'handle_cancel_order', 'handle_cancel_enter', 'handle_cancel_exit',
            'update_trade_state', 'order_obj_or_raise', 'has_unresolved_orders',
        }
        Bot = load_methods('freqtrade/freqtradebot.py', 'FreqtradeBot', methods, ns)
        self.bot = Bot()
        self.bot.config = config
        self.bot.order_journal = self.j
        self.bot.trading_mode = TradingMode.FUTURES
        self.bot.exchange = Mock()
        self.bot.exchange.get_funding_fees.return_value = 0.0
        self.bot.exchange.get_min_pair_stake_amount.return_value = 0.0
        self.bot.exchange.get_precision_amount.return_value = 0.001
        self.bot.exchange.get_contract_size.return_value = 1.0
        self.bot.exchange.precisionMode = constants.DECIMAL_PLACES if hasattr(constants, 'DECIMAL_PLACES') else 4
        self.bot.exchange.check_order_canceled_empty.side_effect = (
            lambda o: o['status'] in constants.CANCELED_EXCHANGE_STATES and (o.get('filled') or 0.0) == 0.0
        )
        self.bot.wallets = Mock()
        self.bot.strategy = SimpleNamespace(
            stoploss=-0.99,
            order_types={'entry': 'limit', 'exit': 'limit', 'force_exit': 'market'},
            order_time_in_force={'entry': 'GTC', 'exit': 'GTC'},
            confirm_trade_exit=lambda **kwargs: True,
            custom_exit_price=lambda **kwargs: kwargs['proposed_rate'],
            ft_check_timed_out=lambda *a: False,
        )
        self.bot.get_valid_price = lambda custom, proposed: custom
        self.bot.cancel_stoploss_on_exchange = lambda trade, **kwargs: trade
        self.bot._safe_exit_amount = lambda trade, pair, amount: min(amount, trade.amount)
        self.bot.handle_order_fee = Mock()
        self.bot._update_trade_after_fill = lambda trade, *a: trade
        self.bot.order_close_notify = Mock()
        self.bot._notify_enter_cancel = Mock()
        self.bot._notify_exit_cancel = Mock()
        self.bot._notify_exit = Mock()
        self.bot._exit_reason_cache = {}
        self.j.ready = True

    def _new_trade(self, orders):
        now = datetime.now(UTC)
        trade = Trade(
            pair=PAIR, stake_amount=200.0, amount=0.0, open_rate=100.0,
            fee_open=0.0, fee_close=0.0, exchange='bybit', is_open=True,
            open_date=now, leverage=1.0, trading_mode=TradingMode.FUTURES,
            contract_size=1.0,
        )
        for o in orders:
            trade.orders.append(o)
        trade.recalc_trade_from_orders()
        Trade.session.add(trade)
        Trade.commit()
        return trade

    def _findings(self):
        return [r[0] for r in self.j.db.execute('SELECT key FROM findings')]

    def _snap(self, open_orders=(), positions=(), orders=None, fills=None):
        from snapshot_fixtures import timed_snapshot
        return timed_snapshot(dict(
            complete=True,
            open_orders=list(open_orders),
            positions=list(positions),
            orders=orders or {},
            fills=fills or {},
        ))

    def test_reconcile_partial_initial_entry_unblocks_reduce_only_exit(self):
        req = dict(pair=PAIR, side='buy', amount=2.0, rate=100.0, leverage=1.0,
                   ordertype='limit', reduceOnly=False, initial_order=True, time_in_force='GTC')
        cid1 = self.j.prepare(req, None)
        o1_raw = dict(id='o1', clientOrderId=cid1, symbol=PAIR, side='buy', status='open',
                      amount=2.0, filled=0.5, remaining=1.5, type='limit', price=100.0, average=100.0)
        self.j.dispatch(cid1, lambda **kw: o1_raw)
        o1 = Order.parse_from_ccxt_object(o1_raw, PAIR, 'buy', 2.0, 100.0)
        trade = self._new_trade([o1])
        self.assertEqual(trade.amount, 0.0)  # recalc_trade_from_orders skips ft_is_open

        self.bot.exchange.fetch_journal_snapshot.return_value = self._snap(
            open_orders=[o1_raw],
            positions=[dict(symbol=PAIR, side='long', amount=0.5, mark_price=100.0)],
            orders={cid1: o1_raw},
            fills={cid1: 0.5},
        )
        reconcile_journal(self.bot, Trade)
        self.assertTrue(self.j.ready)
        self.assertEqual(self._findings(), [])
        # Reduce-only exit must not be blocked while initial entry has open partial fill.
        self.j.prepare(dict(req, side='sell', amount=0.5, reduceOnly=True, initial_order=False), trade.id)

    def test_reconcile_counts_open_partial_dca_and_exit_fills(self):
        from freqtrade.journal_execution import bind_journal
        req = dict(pair=PAIR, side='buy', amount=2.0, rate=100.0, leverage=1.0,
                   ordertype='limit', reduceOnly=False, initial_order=True, time_in_force='GTC')
        cid1 = self.j.prepare(req, None)
        o1_raw = dict(id='o1', clientOrderId=cid1, symbol=PAIR, side='buy', status='closed',
                      amount=2.0, filled=2.0, remaining=0.0, type='limit', price=100.0, average=100.0)
        self.j.dispatch(cid1, lambda **kw: o1_raw)
        o1 = Order.parse_from_ccxt_object(o1_raw, PAIR, 'buy', 2.0, 100.0)
        trade = self._new_trade([o1])
        bind_journal(self.bot, o1_raw['id'], trade.id)
        self.bot.exchange.fetch_journal_snapshot.return_value = self._snap(
            positions=[dict(symbol=PAIR, side='long', amount=2.0, mark_price=100.0)],
            orders={cid1: o1_raw},
            fills={cid1: 2.0},
        )
        reconcile_journal(self.bot, Trade)

        cid2 = self.j.prepare(dict(req, initial_order=False), trade.id)
        o2_raw = dict(id='o2', clientOrderId=cid2, symbol=PAIR, side='buy', status='open',
                      amount=2.0, filled=0.5, remaining=1.5, type='limit', price=95.0, average=95.0)
        self.j.dispatch(cid2, lambda **kw: o2_raw)
        o2 = Order.parse_from_ccxt_object(o2_raw, PAIR, 'buy', 2.0, 95.0)
        trade.orders.append(o2)
        trade.recalc_trade_from_orders()
        Trade.commit()
        self.assertEqual(trade.amount, 2.0)

        self.bot.exchange.fetch_journal_snapshot.return_value = self._snap(
            open_orders=[o2_raw],
            positions=[dict(symbol=PAIR, side='long', amount=2.5, mark_price=98.0)],
            orders={cid1: o1_raw, cid2: o2_raw},
            fills={cid1: 2.0, cid2: 0.5},
        )
        reconcile_journal(self.bot, Trade)
        self.assertTrue(self.j.ready)
        self.assertEqual(self._findings(), [])

        # Canceled DCA keeping its 0.5 partial fill + partial open limit exit (filled=0.5) => live position 2.0
        o2.update_from_ccxt_object({**o2_raw, 'status': 'canceled', 'filled': 0.5, 'remaining': 1.5})
        cid3 = self.j.prepare(dict(req, side='sell', reduceOnly=True, initial_order=False), trade.id)
        o3_raw = dict(id='o3', clientOrderId=cid3, symbol=PAIR, side='sell', status='open',
                      amount=2.0, filled=0.5, remaining=1.5, type='limit', price=105.0, average=105.0)
        self.j.dispatch(cid3, lambda **kw: o3_raw)
        o3 = Order.parse_from_ccxt_object(o3_raw, PAIR, 'sell', 2.0, 105.0)
        trade.orders.append(o3)
        trade.recalc_trade_from_orders()
        Trade.commit()
        self.assertEqual(trade.amount, 2.5)

        self.bot.exchange.fetch_journal_snapshot.return_value = self._snap(
            open_orders=[o3_raw],
            positions=[dict(symbol=PAIR, side='long', amount=2.0, mark_price=104.0)],
            orders={cid1: o1_raw, cid2: {**o2_raw, 'status': 'canceled', 'filled': 0.5, 'remaining': 1.5}, cid3: o3_raw},
            fills={cid1: 2.0, cid2: 0.5, cid3: 0.5},
        )
        reconcile_journal(self.bot, Trade)
        self.assertTrue(self.j.ready)
        self.assertEqual(self._findings(), [])

    def test_execute_exit_recomputes_amount_after_canceling_partial_dca(self):
        entry_raw = dict(id='e1', clientOrderId='ftj-e1', symbol=PAIR, side='buy', status='closed',
                         amount=2.0, filled=2.0, remaining=0.0, type='limit', price=100.0, average=100.0)
        dca_raw = dict(id='d1', clientOrderId='ftj-d1', symbol=PAIR, side='buy', status='open',
                       amount=2.0, filled=0.0, remaining=2.0, type='limit', price=90.0, average=None)
        dca_canceled = {**dca_raw, 'status': 'canceled', 'filled': 1.0, 'remaining': 1.0, 'average': 90.0}
        trade = self._new_trade([
            Order.parse_from_ccxt_object(entry_raw, PAIR, 'buy', 2.0, 100.0),
            Order.parse_from_ccxt_object(dca_raw, PAIR, 'buy', 2.0, 90.0),
        ])
        self.assertEqual(trade.amount, 2.0)
        self.bot.exchange.fetch_order.side_effect = [dca_raw, dca_canceled]
        self.bot.exchange.fetch_journal_snapshot.side_effect = lambda intents: self._snap(
            positions=[dict(symbol=PAIR, side='long', amount=3.0, mark_price=105.0)],
            orders={'ftj-d1': dca_canceled},
            fills={'ftj-d1': 1.0},
        )
        self.bot.exchange.create_order.side_effect = lambda **kw: dict(
            id='ex1', clientOrderId=kw['client_order_id'], symbol=PAIR, side='sell', status='open',
            amount=kw['amount'], filled=0.0, remaining=kw['amount'], type='limit', price=105.0,
        )
        res = self.bot.execute_trade_exit(trade, 105.0, ExitCheckTuple(ExitType.EXIT_SIGNAL))
        self.assertTrue(res)
        self.assertEqual(trade.amount, 3.0)
        self.assertEqual(self.bot.exchange.create_order.call_args.kwargs['amount'], 3.0)

    def test_execute_exit_caps_sub_trade_amt_after_canceling_partial_exit(self):
        entry_raw = dict(id='e1', clientOrderId='ftj-e1', symbol=PAIR, side='buy', status='closed',
                         amount=2.0, filled=2.0, remaining=0.0, type='limit', price=100.0, average=100.0)
        exit_open = dict(id='x1', clientOrderId='ftj-x1', symbol=PAIR, side='sell', status='open',
                         amount=2.0, filled=0.0, remaining=2.0, type='limit', price=102.0, average=None)
        exit_canceled = {**exit_open, 'status': 'canceled', 'filled': 0.5, 'remaining': 1.5, 'average': 102.0}
        trade = self._new_trade([
            Order.parse_from_ccxt_object(entry_raw, PAIR, 'buy', 2.0, 100.0),
            Order.parse_from_ccxt_object(exit_open, PAIR, 'sell', 2.0, 102.0),
        ])
        self.bot.exchange.fetch_order.side_effect = [exit_open, exit_canceled]
        self.bot.exchange.fetch_journal_snapshot.side_effect = lambda intents: self._snap(
            positions=[dict(symbol=PAIR, side='long', amount=1.5, mark_price=105.0)],
            orders={'ftj-x1': exit_canceled},
            fills={'ftj-x1': 0.5},
        )
        self.bot.exchange.create_order.side_effect = lambda **kw: dict(
            id='ex2', clientOrderId=kw['client_order_id'], symbol=PAIR, side='sell', status='open',
            amount=kw['amount'], filled=0.0, remaining=kw['amount'], type='limit', price=105.0,
        )
        res = self.bot.execute_trade_exit(
            trade, 105.0, ExitCheckTuple(ExitType.EXIT_SIGNAL), sub_trade_amt=1.8
        )
        self.assertTrue(res)
        self.assertEqual(trade.amount, 1.5)
        self.assertEqual(self.bot.exchange.create_order.call_args.kwargs['amount'], 1.5)

    def test_execute_exit_aborts_if_open_exit_already_filled_on_cancel(self):
        entry_raw = dict(id='e1', clientOrderId='ftj-e1', symbol=PAIR, side='buy', status='closed',
                         amount=2.0, filled=2.0, remaining=0.0, type='limit', price=100.0, average=100.0)
        exit_open = dict(id='x1', clientOrderId='ftj-x1', symbol=PAIR, side='sell', status='open',
                         amount=2.0, filled=0.0, remaining=2.0, type='limit', price=102.0, average=None)
        exit_filled = {**exit_open, 'status': 'closed', 'filled': 2.0, 'remaining': 0.0, 'average': 102.0}
        trade = self._new_trade([
            Order.parse_from_ccxt_object(entry_raw, PAIR, 'buy', 2.0, 100.0),
            Order.parse_from_ccxt_object(exit_open, PAIR, 'sell', 2.0, 102.0),
        ])
        self.bot.exchange.fetch_order.side_effect = [exit_open, exit_filled]
        res = self.bot.execute_trade_exit(trade, 105.0, ExitCheckTuple(ExitType.EXIT_SIGNAL))
        self.assertFalse(res)
        self.assertFalse(trade.is_open)
        self.bot.exchange.create_order.assert_not_called()

    def test_execute_exit_cancels_empty_entry_and_aborts_on_zero_amount(self):
        req = dict(pair=PAIR, side='buy', amount=2.0, rate=100.0, leverage=1.0,
                   ordertype='limit', reduceOnly=False, initial_order=True, time_in_force='GTC')
        cid = self.j.prepare(req, None)
        entry_open = dict(id='e0', clientOrderId=cid, symbol=PAIR, side='buy', status='open',
                          amount=2.0, filled=0.0, remaining=2.0, type='limit', price=100.0, average=None)
        self.j.dispatch(cid, lambda **kw: entry_open)
        entry_canceled = {**entry_open, 'status': 'canceled', 'filled': 0.0, 'remaining': 2.0}
        trade = self._new_trade([Order.parse_from_ccxt_object(entry_open, PAIR, 'buy', 2.0, 100.0)])
        from freqtrade.journal_execution import bind_journal
        bind_journal(self.bot, entry_open['id'], trade.id)
        self.bot.exchange.fetch_order.side_effect = [entry_open, entry_canceled]
        self.bot.exchange.fetch_journal_snapshot.side_effect = lambda intents: self._snap(
            orders={cid: entry_canceled},
            fills={},
        )
        res = self.bot.execute_trade_exit(trade, 105.0, ExitCheckTuple(ExitType.EXIT_SIGNAL))
        self.assertFalse(res)
        self.assertEqual(trade.amount, 0.0)
        self.assertFalse(trade.has_open_orders)
        self.bot.exchange.create_order.assert_not_called()


if __name__ == '__main__':
    unittest.main()
