"""P0.4 fault, cancellation and worker isolation tests; no exchange/network access."""

import json
import logging
import socket
import sys
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from freqtrade.fault_recovery import cancel_fault_entries
from freqtrade.order_journal import JournalBlocked, OrderJournal
from freqtrade.order_recovery import pending_recovery
from freqtrade.prop_controls import control_status, read_control, request_control, sync_prop_controls
from freqtrade.prop_profile import profile_from_config
from prop_http import DeadlineClient
from prop_risk_guard import RiskGuard, finite_number, market_crash, reconciled_state
import test_order_recovery as recovery_support
from test_order_recovery import Trade, Order, load_methods, snapshot, RPCException
from test_prop_profile import configured_account, baseline


class FaultTests(unittest.TestCase):
    def setUp(self):
        self.recovery = recovery_support.RecoveryTests()
        self.recovery.setUp()
        self.addCleanup(self.recovery.doCleanups)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = datetime.now(UTC)
        self.config = configured_account()
        self.config["user_data_dir"] = self.tmp.name
        self.profile = profile_from_config(self.config)
        self.path = Path(self.tmp.name) / "risk_manager_prop_state.json"
        self.install()
        self.api = Mock()
        self.api.get_prop_profile.return_value = self.profile
        self.api.get_equity.return_value = 5000
        self.api.get_open_trades.return_value = []
        self.api.stop_buying.return_value = True
        self.api.force_exit.return_value = True
        self.api.get_prop_control.return_value = dict(verified=True, flat=True, pending_entries=False, ack=None)
        self.guard = RiskGuard(self.api, self.path, profile=self.profile)
        self.identity = {k: self.profile[k] for k in ("account_id", "challenge_id", "phase_id", "profile_id")}
        self.journal = OrderJournal(Path(self.tmp.name) / "order_journal", self.identity)
        self.addCleanup(lambda: self.journal.close())
        from snapshot_fixtures import timed_snapshot
        self.empty = timed_snapshot(dict(complete=True, open_orders=[], orders={}, fills={}, positions=[]))
        self.journal.reconcile(self.empty, {}, {})
        self.bot = self.recovery.bot
        self.bot.config, self.bot.order_journal = self.config, self.journal
        self.bot.has_unresolved_orders.__func__.__globals__["sync_prop_controls"] = sync_prop_controls
        self.proxy = patch.object(Trade, "get_trades_proxy", lambda: list(Trade.rows.values()), create=True)
        self.proxy.start()
        self.addCleanup(self.proxy.stop)

    def install(self, **updates):
        state = reconciled_state(self.profile, baseline(self.profile, now=self.now), self.now, halted=False)
        state.update(data_fault=False, wake_up_lock=False, data_valid=True)
        state.update(updates)
        self.path.write_text(json.dumps(state))

    def fault(self):
        self.install(data_fault=True, fault_revision=1)
        self.assertTrue(sync_prop_controls(self.bot))

    def snap(self, o, status="open", filled=0):
        return {**snapshot(o, status, filled), "reduceOnly": False}

    def test_equity_unavailable_and_return_does_not_clear_fault(self):
        self.api.get_equity.return_value = None
        self.guard._check_drawdown(self.now)
        self.api.get_equity.return_value = 5000
        self.guard._check_drawdown(self.now)
        self.assertTrue(self.guard.state["data_fault"])
        self.assertTrue(self.guard.state["data_valid"])
        self.api.start_trading.assert_not_called()
        restored = RiskGuard(self.api, self.path, profile=self.profile)
        self.assertTrue(restored.state["data_fault"])

    def test_core_stale_heartbeat_latches_across_restart(self):
        self.install(last_updated=(self.now - timedelta(seconds=16)).isoformat())
        self.assertTrue(sync_prop_controls(self.bot))
        self.install()
        self.journal.close()
        self.journal = OrderJournal(Path(self.tmp.name) / "order_journal", self.identity)
        self.bot.order_journal = self.journal
        self.assertTrue(sync_prop_controls(self.bot))
        self.assertTrue(read_control(self.journal)["data_fault"])

    def test_worker_fault_survives_before_supervisor_file_write(self):
        original = self.path.read_text()
        self.guard.request_data_fault("Worker disconnected")
        self.assertEqual(self.path.read_text(), original)
        self.journal.close()
        self.journal = OrderJournal(Path(self.tmp.name) / "order_journal", self.identity)
        self.bot.order_journal = self.journal
        self.assertTrue(sync_prop_controls(self.bot))
        self.assertEqual(self.journal.db.execute("SELECT acknowledged FROM fault_events").fetchone()[0], 0)

    def test_fault_during_equity_request_cannot_be_overwritten(self):
        def interrupted():
            self.guard.request_data_fault("Concurrent macro fault")
            return 5000
        self.api.get_equity.side_effect = interrupted
        self.guard._check_drawdown(self.now)
        self.assertTrue(self.guard.state["data_fault"])
        self.assertFalse(self.guard.state["data_valid"])

    def test_authenticated_ack_requires_both_supervisor_and_core(self):
        self.api.get_prop_control.side_effect = lambda: control_status(self.bot, Trade, Order)
        self.api.get_equity.return_value = None
        self.guard._check_drawdown(self.now)
        sync_prop_controls(self.bot)
        self.api.get_equity.return_value = 5000
        self.guard._check_drawdown(self.now)
        result = request_control(self.bot, Trade, Order, "acknowledge", "Operator verified account")
        self.assertTrue(sync_prop_controls(self.bot))
        self.guard._check_drawdown(self.now)
        self.assertFalse(self.guard.state["data_fault"])
        self.assertEqual(self.guard.state["acknowledged_control"], result["ack"]["id"])
        control_status(self.bot, Trade, Order)
        self.assertFalse(sync_prop_controls(self.bot))
        self.api.start_trading.assert_not_called()
        self.assertGreater(self.journal.db.execute("SELECT count(*) FROM control_audit").fetchone()[0], 0)

    def test_new_fault_invalidates_pending_ack(self):
        self.api.get_prop_control.side_effect = lambda: control_status(self.bot, Trade, Order)
        self.fault()
        request_control(self.bot, Trade, Order, "acknowledge", "Reviewed")
        self.guard.request_data_fault("New connection loss")
        self.guard.request_data_fault("New connection loss after review")
        self.guard._check_drawdown(self.now)
        self.assertTrue(self.guard.state["data_fault"])
        self.assertIsNone(read_control(self.journal)["ack"])

    def test_ack_does_not_clear_before_fresh_core_reverification(self):
        self.api.get_prop_control.side_effect = lambda: control_status(self.bot, Trade, Order)
        self.guard.request_data_fault("Fault")
        self.guard._check_drawdown(self.now)
        request_control(self.bot, Trade, Order, "acknowledge", "Reviewed")
        self.guard._check_drawdown(self.now)
        self.assertFalse(self.guard.state["data_fault"])
        self.journal.ready = False  # A new unresolved operation invalidates the proof.
        control_status(self.bot, Trade, Order)
        self.assertTrue(read_control(self.journal)["data_fault"])
        self.journal.reconcile(self.empty, {}, {})
        control_status(self.bot, Trade, Order)
        self.assertFalse(read_control(self.journal)["data_fault"])

    def test_expired_ack_cannot_unlock(self):
        self.fault()
        request_control(self.bot, Trade, Order, "acknowledge", "Reviewed")
        state = read_control(self.journal)
        state["ack"]["created_ms"] -= 61000
        with self.journal.db:
            self.journal.db.execute("UPDATE control SET value=?", (json.dumps(state),))
        self.install(acknowledged_control=state["ack"]["id"])
        control_status(self.bot, Trade, Order)
        self.assertTrue(read_control(self.journal)["data_fault"])

    def test_ack_refused_with_unresolved_reconciliation_or_risk_halt(self):
        self.fault()
        self.journal.ready = False
        with self.assertRaises(JournalBlocked):
            request_control(self.bot, Trade, Order, "acknowledge", "Reviewed")
        self.journal.reconcile(self.empty, {}, {})
        self.install(nuclear_triggered=True)
        with self.assertRaises(JournalBlocked):
            request_control(self.bot, Trade, Order, "acknowledge", "Reviewed")
        self.assertTrue(read_control(self.journal)["risk_halt"])

    def test_start_cannot_bypass_persistent_fault_even_if_already_running(self):
        from freqtrade.enums import State
        Rpc = load_methods("freqtrade/rpc/rpc.py", "RPC", {"_rpc_start"},
                           dict(State=State, RPCException=RPCException))
        rpc = Rpc()
        rpc._freqtrade = self.bot
        self.bot.state = State.RUNNING
        self.fault()
        with self.assertRaises(RPCException):
            rpc._rpc_start()

    def test_manual_lock_survives_macro_recovery(self):
        request_control(self.bot, Trade, Order, "lock", "Maintenance")
        self.api.get_prop_control.side_effect = lambda: control_status(self.bot, Trade, Order)
        self.guard.request_macro_pause(True)
        self.guard._check_drawdown(self.now)
        self.guard.request_macro_pause(False)
        self.guard._check_drawdown(self.now)
        self.api.start_trading.assert_not_called()
        self.assertTrue(read_control(self.journal)["manual_lock"])

    def test_macro_only_pause_can_resume(self):
        self.api.get_prop_control.side_effect = lambda: control_status(self.bot, Trade, Order)
        self.guard.request_macro_pause(True)
        self.guard._check_drawdown(self.now)
        self.assertTrue(read_control(self.journal)["macro_paused"])
        self.guard.request_macro_pause(False)
        self.guard._check_drawdown(self.now)
        self.api.start_trading.assert_called_once()

    def test_macro_cannot_resume_risk_halt_and_local_flat_is_insufficient(self):
        self.guard.request_macro_pause(True)
        self.guard._check_drawdown(self.now)
        self.api.get_prop_control.return_value = dict(verified=True, flat=False, pending_entries=False)
        self.guard.trigger_nuclear_option("Drawdown")
        self.guard.request_macro_pause(False)
        self.guard._check_drawdown(self.now)
        self.assertFalse(self.guard.state["liquidation_confirmed"])
        self.api.start_trading.assert_not_called()

    def test_limit_entry_cancelled_with_terminal_lookup(self):
        self.fault()
        trade, o = self.recovery.trade_order(base=0)
        self.bot.exchange.fetch_order.side_effect = [self.snap(o), self.snap(o, "canceled")]
        cancel_fault_entries(self.bot, Trade)
        self.bot.exchange.cancel_order.assert_called_once_with(o.order_id, trade.pair)
        self.assertEqual(self.journal.db.execute("SELECT state FROM cancellations").fetchone()[0], "confirmed")
        # Terminal cancellation alone does not establish account-wide absence of exposure.
        self.assertIn(trade.id, Trade.rows)
        self.bot.exchange.fetch_journal_snapshot.return_value = self.empty
        self.bot.manage_open_orders()
        self.assertNotIn(trade.id, Trade.rows)
        self.assertTrue(sync_prop_controls(self.bot))

    def test_fill_during_cancel_is_accounted(self):
        self.fault()
        trade, o = self.recovery.trade_order()
        self.bot.exchange.fetch_order.side_effect = [self.snap(o), self.snap(o, "canceled", 1)]
        cancel_fault_entries(self.bot, Trade)
        self.assertEqual(trade.amount, 11)
        self.assertEqual(o.filled, 1)
        self.assertEqual(pending_recovery(trade), [])

    def test_full_fill_during_cancel_is_accounted(self):
        self.fault()
        trade, o = self.recovery.trade_order()
        self.bot.exchange.fetch_order.side_effect = [self.snap(o), self.snap(o, "closed", 2)]
        cancel_fault_entries(self.bot, Trade)
        self.assertEqual(trade.amount, 12)

    def test_cancel_timeout_can_be_confirmed_only_by_terminal_read(self):
        from freqtrade.exceptions import ExchangeError
        self.fault()
        trade, o = self.recovery.trade_order()
        self.bot.exchange.cancel_order.side_effect = ExchangeError("Response lost")
        self.bot.exchange.fetch_order.side_effect = [self.snap(o), self.snap(o, "canceled")]
        cancel_fault_entries(self.bot, Trade)
        self.assertEqual(pending_recovery(trade), [])
        self.assertFalse(o.ft_is_open)

    def test_cancel_timeout_and_inconclusive_read_remain_persistent(self):
        from freqtrade.exceptions import ExchangeError
        self.fault()
        trade, o = self.recovery.trade_order()
        self.bot.exchange.cancel_order.side_effect = ExchangeError("timeout")
        self.bot.exchange.fetch_order.side_effect = [self.snap(o), {}]
        cancel_fault_entries(self.bot, Trade)
        self.assertIn(o.order_id, pending_recovery(trade))
        self.assertTrue(o.ft_is_open)
        self.journal.close()
        self.journal = OrderJournal(Path(self.tmp.name) / "order_journal", self.identity)
        self.assertEqual(self.journal.db.execute("SELECT state FROM cancellations").fetchone()[0], "uncertain")
        self.bot.exchange.create_order.assert_not_called()

    def test_open_after_cancel_is_not_success(self):
        self.fault()
        trade, o = self.recovery.trade_order()
        self.bot.exchange.fetch_order.return_value = self.snap(o)
        cancel_fault_entries(self.bot, Trade)
        self.assertIn(o.order_id, pending_recovery(trade))
        self.assertEqual(self.journal.db.execute("SELECT state FROM cancellations").fetchone()[0], "uncertain")

    def test_reduce_only_order_is_not_cancelled(self):
        self.fault()
        trade, o = self.recovery.trade_order(side="sell")
        cancel_fault_entries(self.bot, Trade)
        self.bot.exchange.cancel_order.assert_not_called()
        self.assertTrue(o.ft_is_open)

    def test_unverifiable_reduce_only_flag_is_not_cancelled(self):
        self.fault()
        trade, o = self.recovery.trade_order()
        self.bot.exchange.fetch_order.return_value = {**self.snap(o), "reduceOnly": True}
        cancel_fault_entries(self.bot, Trade)
        self.bot.exchange.cancel_order.assert_not_called()
        self.assertIn(o.order_id, pending_recovery(trade))

    def test_existing_position_can_exit_under_entry_fault(self):
        from freqtrade.journal_execution import journal_create_order
        self.fault()
        trade = Trade()
        self.bot.exchange.fetch_journal_snapshot.return_value = {**self.empty,
            "positions": [dict(symbol=trade.pair, side="long", amount=trade.amount)]}
        self.bot.exchange.create_order.side_effect = lambda **kw: dict(id="exit", clientOrderId=kw["client_order_id"])
        result = journal_create_order(self.bot, trade, Trade, pair=trade.pair, side="sell", amount=10,
            rate=100, leverage=1, ordertype="market", reduceOnly=True, initial_order=False)
        self.assertEqual(result["id"], "exit")
        self.assertTrue(read_control(self.journal)["data_fault"])

    def test_cancellation_and_journal_reconcile_without_duplicate_submission(self):
        from snapshot_fixtures import timed_snapshot
        self.fault()
        trade, o = self.recovery.trade_order(base=0)
        request = dict(pair=trade.pair, side="buy", amount=2, rate=100, leverage=1,
                       ordertype="limit", reduceOnly=False, initial_order=True)
        cid = self.journal.prepare(request, trade.id)
        opened = {**self.snap(o), "clientOrderId": cid}
        canceled = {**self.snap(o, "canceled"), "clientOrderId": cid}
        self.journal.dispatch(cid, Mock(return_value=opened))
        self.bot.exchange.fetch_order.side_effect = [opened, canceled]
        cancel_fault_entries(self.bot, Trade)
        evidence = timed_snapshot({**self.empty, "orders": {cid: canceled}, "fills": {cid: 0}})
        self.assertEqual(self.journal.reconcile(evidence, {}, {}), [])
        self.assertEqual(self.journal.intents(), [])
        self.bot.exchange.create_order.assert_not_called()
        self.assertTrue(sync_prop_controls(self.bot))

    def test_no_risk_state_lock_held_during_http_or_disk(self):
        def equity():
            self.assertFalse(self.guard._state_lock._is_owned())
            return 5000
        self.api.get_equity.side_effect = equity
        import os
        fsync = os.fsync
        def check_disk(fd):
            self.assertFalse(self.guard._state_lock._is_owned())
            return fsync(fd)
        with patch("prop_risk_guard.os.fsync", side_effect=check_disk):
            self.guard._check_drawdown(self.now)

    def test_simultaneous_pause_resume_and_fault_preserves_fault(self):
        barrier = threading.Barrier(3)
        def request(paused):
            barrier.wait()
            self.guard.request_macro_pause(paused)
        threads = [threading.Thread(target=request, args=(p,)) for p in (True, False)]
        for thread in threads:
            thread.start()
        barrier.wait()
        self.guard.request_data_fault("Connection interrupted")
        for thread in threads:
            thread.join(1)
        self.guard._check_drawdown(self.now)
        self.assertTrue(self.guard.state["data_fault"])
        self.api.start_trading.assert_not_called()

    def test_blocked_macro_worker_does_not_block_equity(self):
        self._blocked_worker("_macro_loop")

    def test_blocked_accounting_worker_does_not_block_equity(self):
        self._blocked_worker("_hyrotrader_rules_loop")

    def _blocked_worker(self, method):
        Worker = load_methods("scripts/llm_risk_manager_prop.py", "RiskManager", {method},
            dict(logger=logging.getLogger(__name__), market_crash=market_crash, finite_number=finite_number,
                 datetime=datetime, timezone=timezone))
        entered, release = threading.Event(), threading.Event()
        def slow():
            entered.set()
            release.wait(2)
            self.guard._shutdown.set()
            return {} if method == "_macro_loop" else []
        self.guard.macro_monitor = SimpleNamespace(get_metrics=slow)
        self.guard.rules_api = SimpleNamespace(get_open_trades=slow, get_trades=lambda **kwargs: [])
        thread = threading.Thread(target=lambda: getattr(Worker, method)(self.guard))
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            self.guard._check_drawdown(self.now)
            self.assertLess(time.monotonic() - started, 1)
            self.api.get_equity.assert_called_once()
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())


class HttpTests(unittest.TestCase):
    def setUp(self):
        network = patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        self.client = DeadlineClient("http://offline", "user", "password")
        self.client.access_token = "test-token"
        self.client.session = Mock(headers={})
        self.clock = [100.0]
        monotonic = patch("prop_http.time.monotonic", side_effect=lambda: self.clock[0])
        monotonic.start()
        self.addCleanup(monotonic.stop)
        sleep = patch("prop_http.time.sleep", side_effect=lambda delay: self.clock.__setitem__(0, self.clock[0] + delay))
        sleep.start()
        self.addCleanup(sleep.stop)

    @staticmethod
    def response(payload, status=200):
        response = Mock(status_code=status)
        response.iter_content.return_value = [json.dumps(payload).encode()]
        class Response:
            def __enter__(self): return response
            def __exit__(self, *args): return False
        return Response()

    def test_explicit_timeouts_and_independent_sessions(self):
        other = DeadlineClient("http://offline", "user", "password")
        self.addCleanup(other.session.close)
        self.assertIsNot(self.client.session, other.session)
        self.client.session.request.return_value = self.response({"ok": True})
        self.assertEqual(self.client.get("/balance"), {"ok": True})
        timeout = self.client.session.request.call_args.kwargs["timeout"]
        self.assertEqual(timeout.total, 4.0)
        self.assertEqual(timeout.connect_timeout, 1.0)
        self.assertEqual(timeout.read_timeout, 1.5)

    def test_late_response_discarded_and_budget_not_extended(self):
        def slow(*args, **kwargs):
            self.clock[0] += 5
            return self.response({"ok": True})
        self.client.session.request.side_effect = slow
        with self.client.operation_budget(4):
            self.assertIsNone(self.client.get("/balance"))
            self.assertIsNone(self.client.post("/start"))
        self.assertEqual(self.client.session.request.call_count, 1)

    def test_slow_refresh_cannot_extend_budget_or_send_command(self):
        self.client.token_expiry = datetime.now(timezone.utc) - timedelta(seconds=1)
        def slow(*args, **kwargs):
            self.clock[0] += 5
            return self.response({"access_token": "late"})
        self.client.session.request.side_effect = slow
        self.assertIsNone(self.client.post("/start"))
        self.assertEqual(self.client.session.request.call_count, 1)
        self.assertTrue(self.client.session.request.call_args.args[1].endswith("/token/refresh"))

    def test_failed_refresh_and_login_do_not_send_command(self):
        self.client.token_expiry = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.client.session.request.side_effect = OSError("offline")
        self.assertIsNone(self.client.post("/start"))
        urls = [c.args[1] for c in self.client.session.request.call_args_list]
        self.assertFalse(any(u.endswith("/start") for u in urls))

    def test_read_retry_backoff_is_inside_budget(self):
        self.client.session.request.side_effect = OSError("offline")
        self.assertIsNone(self.client.get("/balance"))
        self.assertEqual(self.client.session.request.call_count, 3)
        self.assertAlmostEqual(self.clock[0], 100.3)

    def test_ambiguous_commands_are_not_retried(self):
        self.client.session.request.side_effect = OSError("lost reply")
        self.assertFalse(self.client.execute_critical_command("EXIT", "POST", "/forceexit"))
        self.client.session.request.assert_called_once()

    def test_sessions_reject_cross_worker_sharing(self):
        with self.client.operation_budget():
            pass
        errors = []
        def other():
            try:
                self.client.get("/balance")
            except RuntimeError as exc:
                errors.append(exc)
        thread = threading.Thread(target=other)
        thread.start()
        thread.join(1)
        self.assertEqual(len(errors), 1)
        self.client.session.request.assert_not_called()

    def test_stale_equity_rejected_by_actual_client_method(self):
        Client = load_methods("scripts/llm_risk_manager_prop.py", "FreqtradeAPIClient", {"get_equity"},
                              dict(finite_number=finite_number, time=time))
        client = Client()
        client.get_balance = Mock(return_value={"account_equity": {
            "value": 5000, "timestamp": time.time() * 1000 - 16000,
            "currency": "USDT", "source": "bybit_wallet_coin_equity"}})
        self.assertIsNone(client.get_equity())

    def test_get_trades_pagination_and_rules_loop_no_forceentry(self):
        Client = load_methods("scripts/llm_risk_manager_prop.py", "FreqtradeAPIClient", {"get_trades"}, {})
        client = Client()
        client.get = Mock(side_effect=[
            {"trades": [{"trade_id": 1}, {"trade_id": 2}], "total_trades": 3},
            {"trades": [{"trade_id": 3}], "total_trades": 3},
        ])
        self.assertEqual(client.get_trades(limit=2), [{"trade_id": 1}, {"trade_id": 2}, {"trade_id": 3}])
        self.assertEqual(client.get.call_args_list[0].args[0], "/trades?limit=2")
        self.assertEqual(client.get.call_args_list[1].args[0], "/trades?limit=2&offset=2")

        Worker = load_methods("scripts/llm_risk_manager_prop.py", "RiskManager", {"_hyrotrader_rules_loop"},
            dict(logger=logging.getLogger(__name__), datetime=datetime, timezone=timezone))
        old_close = int((datetime.now(timezone.utc) - timedelta(days=25)).timestamp() * 1000)
        post_mock = Mock()
        mgr = SimpleNamespace(
            _shutdown=SimpleNamespace(is_set=Mock(side_effect=[False, True]), wait=Mock()),
            rules_api=SimpleNamespace(
                get_open_trades=lambda: [],
                get_trades=lambda **kw: [{"close_timestamp": old_close, "profit_abs": 10.0, "cost": 300.0, "profit_ratio": 0.03}],
                post=post_mock,
            ),
            nuclear_triggered=False,
            initial_balance=5000,
            profile={"profit_target_percent": 0.1, "daily_profit_cap_percent": 0.4, "min_trading_days": 5},
            request_data_fault=Mock(),
        )
        Worker._hyrotrader_rules_loop(mgr)
        post_mock.assert_not_called()
        mgr.request_data_fault.assert_not_called()

        mgr.inactivity_keepalive_enabled = True
        mgr._shutdown.is_set.side_effect = [False, True]
        Worker._hyrotrader_rules_loop(mgr)
        post_mock.assert_not_called()

        # When keep-alive is explicitly enabled AND state is healthy, verify keyword json= and strict trade_id
        post_mock.return_value = {"trade_id": 99}
        force_exit_mock = Mock(return_value=True)
        mgr.rules_api.force_exit = force_exit_mock
        mgr.state = {"data_valid": True, "data_fault": False, "wake_up_lock": False}
        mgr._shutdown.is_set.side_effect = [False, True]
        Worker._hyrotrader_rules_loop(mgr)
        post_mock.assert_called_once_with(
            "/forceenter",
            json={"pair": "BTC/USDT:USDT", "side": "long", "ordertype": "limit", "stakeamount": 15.0},
        )
        force_exit_mock.assert_called_once_with(99)

    def test_rpc_delete_and_cancel_open_order_blocked_under_prop_profile(self):
        from freqtrade.enums import State
        trade = SimpleNamespace(id=1, is_open=True, open_orders=[], pair="BTC/USDT:USDT")
        FakeTrade = SimpleNamespace(
            id=1,
            get_trades=lambda trade_filter=None: SimpleNamespace(first=lambda: trade),
        )
        Rpc = load_methods(
            "freqtrade/rpc/rpc.py",
            "RPC",
            {"_rpc_delete", "_rpc_cancel_open_order"},
            dict(
                State=State,
                RPCException=RPCException,
                Trade=FakeTrade,
                logger=logging.getLogger(__name__),
                pending_recovery=lambda t: [],
            ),
        )
        rpc = Rpc()
        bot = SimpleNamespace(
            state=State.RUNNING,
            _exit_lock=threading.Lock(),
            config={"prop_guard_enabled": True},
            order_journal=None,
            handle_cancel_order=Mock(),
        )
        rpc._freqtrade = bot
        with self.assertRaisesRegex(RPCException, "disabled under prop trading profile"):
            rpc._rpc_cancel_open_order(1)
        bot.handle_cancel_order.assert_not_called()

        with self.assertRaisesRegex(RPCException, "Cannot delete active or unsettled trade"):
            rpc._rpc_delete(trade.id)


if __name__ == "__main__":
    unittest.main()
