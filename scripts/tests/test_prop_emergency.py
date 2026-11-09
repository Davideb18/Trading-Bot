"""P0.5: deterministic exchange doubles, real journal/OS locks, no operational credentials."""

import hashlib
import json
import socket
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from freqtrade.emergency_authority import EmergencyTakeover, read_emergency, require_core_authority
from freqtrade.emergency_supervisor import EmergencySupervisor
from freqtrade.journal_execution import open_journal
from freqtrade.order_journal import JournalBlocked, OrderJournal
from freqtrade.prop_profile import profile_from_config
from freqtrade.supervisor_bybit import AccountMismatch, SupervisorBybit, UnverifiedAccount
from prop_independent import IndependentRiskAPI
from prop_risk_guard import RiskGuard, reconciled_state
from test_prop_profile import configured_account, baseline


PAIR = "BTC/USDT:USDT"
KEY = "offline-fake-key"


def order(cid, *, side="buy", amount=2.0, filled=2.0, status="closed", reduce=False):
    return {"id": "id-" + cid, "clientOrderId": cid, "symbol": PAIR, "side": side,
            "amount": amount, "filled": filled, "remaining": 0 if status == "closed" else amount-filled,
            "status": status, "price": 100., "average": 100. if filled else None, "reduceOnly": reduce}


class FakeBybit:
    operation_budget = SupervisorBybit.operation_budget

    def __init__(self, profile):
        self.deadline = None
        self.profile = profile
        self.amount = 2.0
        self.orders = {}
        self.behavior = "filled"
        self.sent = []
        self.canceled = []
        self.equity_value = 4820
        self.cancel_fill = 0

    def verify_identity(self): return self.profile
    def equity(self): return self.equity_value
    def expected_amount(self, request): return request["amount"]

    def snapshot(self):
        return {"equity": self.equity_value, "checked_ms": int(time.time()*1000),
                "orders": [dict(o) for o in self.orders.values() if o["status"] == "open"],
                "positions": ([{"pair": PAIR, "side": "buy" if self.amount > 0 else "sell",
                    "amount": abs(self.amount), "contracts": abs(self.amount), "mark_price": 100.}]
                              if self.amount else [])}

    def lookup(self, pair, *, client_id=None, order_id=None):
        o = self.orders.get(client_id)
        return dict(o) if o else None

    def close_request(self, p):
        return dict(pair=p["pair"], side="sell" if p["side"] == "buy" else "buy", amount=p["amount"],
                    ordertype="market", reduceOnly=True)

    def create_order(self, *, client_order_id, **request):
        self.sent.append((client_order_id, request))
        if self.behavior == "unknown":
            raise UnverifiedAccount("Lost response, execution unknown")
        amount = request["amount"]
        filled = 0 if self.behavior == "rejected" else amount/2 if self.behavior in ("partial", "partial_cancel") else amount
        status = "rejected" if self.behavior == "rejected" else "open" if self.behavior == "partial" else "canceled" if self.behavior == "partial_cancel" else "closed"
        o = order(client_order_id, side=request["side"], amount=amount, filled=filled, status=status, reduce=True)
        self.orders[client_order_id] = o
        self.amount += filled * (1 if request["side"] == "buy" else -1)
        if self.behavior == "crash":
            raise KeyboardInterrupt()
        if self.behavior == "lost":
            raise UnverifiedAccount("Accepted; response lost")
        return o

    def cancel_entry(self, o):
        self.canceled.append(o["id"])
        source = self.orders[o["clientOrderId"]]
        source.update(status="canceled", filled=self.cancel_fill, remaining=source["amount"]-self.cancel_fill)
        self.amount += self.cancel_fill * (1 if source["side"] == "buy" else -1)


class EmergencyTests(unittest.TestCase):
    def setUp(self):
        net = patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden"))
        net.start()
        self.addCleanup(net.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.directory = self.root / "order_journal"
        self.config = configured_account()
        self.config["prop_guard"]["account_id"] = "123"
        self.config.update(user_data_dir=str(self.root), db_url="sqlite:////missing/trades.sqlite")
        self.profile = profile_from_config(self.config)
        self.binding = {k: self.profile[k] for k in ("account_id", "challenge_id", "phase_id", "profile_id",
                                                    "exchange", "environment", "currency")}
        self.binding.update(key_fingerprint=hashlib.sha256(KEY.encode()).hexdigest(), database=self.config["db_url"])
        self.state_file = self.root / "risk_manager_prop_state.json"
        self.now = datetime.now(UTC)
        state = reconciled_state(self.profile, baseline(self.profile, now=self.now), self.now, halted=True)
        state.update(halt_reason="Internal static daily floor reached")
        self.state_file.write_text(json.dumps(state))
        journal = OrderJournal(self.directory, self.binding)
        self.seed(journal, "entry", order("entry"))
        journal.close()
        self.adapter = FakeBybit(self.profile)
        self.supervisor = self.new_supervisor()
        self.addCleanup(lambda: self.supervisor.close())

    def seed(self, journal, cid, o, settled=True):
        request = dict(pair=PAIR, side=o["side"], amount=o["amount"], ordertype="limit", reduceOnly=o["reduceOnly"])
        with journal.db:
            journal.db.execute("INSERT INTO intents(client_id,created_ms,request,state,exchange_id,evidence,settled) VALUES (?,?,?,?,?,?,?)",
                (cid, int(time.time()*1000), json.dumps(request), "accepted", o["id"], json.dumps(o), int(settled)))

    def new_supervisor(self, **kwargs):
        return EmergencySupervisor(self.adapter, self.directory, self.state_file, self.profile, KEY,
                                   coordinated=kwargs.get("coordinated", True))

    def state(self): return read_emergency(self.directory)

    def test_liquidation_and_flat_without_freqtrade_or_database(self):
        self.assertFalse(self.supervisor.step())
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertTrue(self.adapter.sent[0][1]["reduceOnly"])
        self.assertFalse(self.state()["confirmed"])
        self.assertTrue(self.supervisor.step())
        self.assertEqual(self.state()["phase"], "flat_confirmed")
        self.assertTrue(self.state()["db_reconciliation_required"])
        self.assertTrue(self.state()["handoff"])

    def test_timeout_after_acceptance_is_reconciled_without_duplicate(self):
        self.adapter.behavior = "lost"
        self.assertFalse(self.supervisor.step())
        cid = self.adapter.sent[0][0]
        self.assertEqual(self.supervisor.journal.intents()[0]["state"], "uncertain")
        self.assertTrue(self.supervisor.step())
        self.assertEqual([x[0] for x in self.adapter.sent], [cid])

    def test_unknown_submission_never_retried_even_if_account_appears_flat(self):
        self.adapter.behavior = "unknown"
        self.supervisor.step()
        self.adapter.amount = 0
        for _ in range(3):
            self.assertFalse(self.supervisor.step())
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertFalse(self.state()["confirmed"])

    def test_partial_open_order_prevents_second_submission(self):
        self.adapter.behavior = "partial"
        self.supervisor.step()
        self.assertEqual(self.adapter.amount, 1)
        self.assertFalse(self.supervisor.step())
        self.assertEqual(len(self.adapter.sent), 1)
        cid = self.adapter.sent[0][0]
        self.adapter.orders[cid].update(status="closed", filled=2, remaining=0)
        self.adapter.amount = 0
        self.assertTrue(self.supervisor.step())

    def test_partial_terminal_order_closes_only_reverified_residual(self):
        self.adapter.behavior = "partial_cancel"
        self.supervisor.step()
        self.adapter.behavior = "filled"
        self.assertFalse(self.supervisor.step())
        self.assertEqual([r["amount"] for _, r in self.adapter.sent], [2, 1])
        self.assertTrue(self.supervisor.step())

    def test_rejected_order_requires_lookup_before_new_intent(self):
        self.adapter.behavior = "rejected"
        self.supervisor.step()
        self.adapter.behavior = "filled"
        self.supervisor.step()
        self.assertEqual(len(self.adapter.sent), 2)
        self.assertNotEqual(self.adapter.sent[0][0], self.adapter.sent[1][0])
        self.assertTrue(self.supervisor.step())

    def test_restart_after_crash_uses_same_intent(self):
        self.adapter.behavior = "crash"
        with self.assertRaises(KeyboardInterrupt):
            self.supervisor.step()
        cid = self.adapter.sent[0][0]
        self.supervisor.close()
        self.supervisor = self.new_supervisor()
        self.assertTrue(self.supervisor.step())
        self.assertEqual([x[0] for x in self.adapter.sent], [cid])

    def test_core_owner_must_exit_before_supervisor_can_submit(self):
        core = OrderJournal(self.directory, self.binding)
        self.addCleanup(core.close)
        self.assertFalse(self.supervisor.step())
        self.assertEqual(self.adapter.sent, [])
        bot = SimpleNamespace(config=self.config, order_journal=core)
        with self.assertRaises(EmergencyTakeover):
            require_core_authority(bot)
        self.assertTrue(bot._emergency_handoff)
        core.close()  # Simulated completed process termination, not lease stealing.
        self.supervisor.step()
        self.assertEqual(len(self.adapter.sent), 1)

    def test_takeover_escapes_worker_stop_state_handler(self):
        from freqtrade.exceptions import OperationalException, TemporaryError
        from test_order_recovery import load_methods
        Worker = load_methods("freqtrade/worker.py", "Worker", {"_process_running"},
                              dict(OperationalException=OperationalException, TemporaryError=TemporaryError))
        worker = Worker()
        worker.freqtrade = SimpleNamespace(process=Mock(side_effect=EmergencyTakeover("handoff")))
        with self.assertRaises(EmergencyTakeover): worker._process_running()

    def test_emergency_cleanup_does_not_cancel_protections_or_release_early(self):
        import logging
        from test_order_recovery import load_methods
        core = OrderJournal(self.directory, self.binding)
        self.addCleanup(core.close)
        self.supervisor.step()
        Bot = load_methods("freqtrade/freqtradebot.py", "FreqtradeBot", {"cleanup"}, dict(
            logger=logging.getLogger(__name__), require_core_authority=require_core_authority,
            EmergencyTakeover=EmergencyTakeover, Trade=SimpleNamespace(session=True, commit=Mock())))
        bot = Bot()
        bot.config = {**self.config, "cancel_open_orders_on_exit": True}
        bot.order_journal = core
        bot.exchange, bot.rpc, bot.strategy = Mock(), Mock(), Mock()
        bot.emc = None
        bot.cancel_all_open_orders, bot.check_for_open_trades = Mock(), Mock()
        bot.cleanup()
        bot.cancel_all_open_orders.assert_not_called()
        self.assertTrue(bot._emergency_handoff)
        with self.assertRaises(JournalBlocked): OrderJournal(self.directory, self.binding)
        self.assertIsNotNone(core.db.execute("SELECT 1").fetchone())

    def test_restart_core_refused_even_after_supervisor_shutdown(self):
        self.supervisor.step()
        self.supervisor.step()
        self.supervisor.close()
        with self.assertRaises(EmergencyTakeover):
            open_journal(self.config, {"key": KEY})

    def test_two_supervisors_cannot_own_same_volume(self):
        with self.assertRaises(JournalBlocked):
            self.new_supervisor()

    def test_supervisor_lock_excludes_a_second_process(self):
        import subprocess
        script = """
import json, sys
from freqtrade.emergency_supervisor import EmergencySupervisor
from freqtrade.order_journal import JournalBlocked
try:
    EmergencySupervisor(None, sys.argv[1], sys.argv[2], json.loads(sys.argv[3]), sys.argv[4])
except JournalBlocked:
    sys.exit(23)
"""
        result = subprocess.run([sys.executable, "-B", "-c", script, str(self.directory),
                                 str(self.state_file), json.dumps(self.profile), KEY],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 23, result.stderr)

    def test_no_local_coordination_attestation_means_no_mutations(self):
        self.supervisor.close()
        self.supervisor = self.new_supervisor(coordinated=False)
        self.assertFalse(self.supervisor.step())
        self.assertEqual(self.adapter.sent, [])
        self.assertFalse(self.state()["handoff"])

    def test_no_persisted_halt_cannot_trade(self):
        state = json.loads(self.state_file.read_text())
        state["nuclear_triggered"] = False
        self.state_file.write_text(json.dumps(state))
        self.assertFalse(self.supervisor.step())
        self.assertEqual(self.adapter.sent, [])

    def test_changed_external_position_is_not_attributed(self):
        self.adapter.amount = 3
        self.assertFalse(self.supervisor.step())
        self.assertEqual(self.adapter.sent, [])
        self.assertIn("provenance", self.state()["detail"])

    def test_external_entry_is_not_cancelled_or_claimed(self):
        self.adapter.orders["external"] = order("external", status="open", filled=0)
        self.assertFalse(self.supervisor.step())
        self.assertEqual(self.adapter.canceled, [])
        self.assertEqual(self.adapter.sent, [])

    def test_entry_fill_during_cancel_is_included_in_liquidation(self):
        journal = OrderJournal(self.directory, self.binding)
        entry = order("pending", status="open", filled=0, amount=1)
        self.seed(journal, "pending", entry, settled=False)
        journal.close()
        self.adapter.orders["pending"] = entry
        self.adapter.cancel_fill = .5
        self.assertFalse(self.supervisor.step())
        self.assertEqual(self.adapter.amount, 2.5)
        self.assertEqual(self.adapter.canceled, [entry["id"]])
        self.supervisor.step()
        self.assertEqual(self.adapter.sent[0][1]["amount"], 2.5)
        self.assertTrue(self.supervisor.step())

    def test_lost_cancel_response_requires_terminal_lookup(self):
        journal = OrderJournal(self.directory, self.binding)
        entry = order("pending", status="open", filled=0, amount=1)
        self.seed(journal, "pending", entry, settled=False)
        journal.close()
        self.adapter.orders["pending"] = entry
        cancel = self.adapter.cancel_entry
        def lost(o):
            cancel(o)
            raise UnverifiedAccount("Cancel response lost")
        self.adapter.cancel_entry = lost
        self.assertFalse(self.supervisor.step())
        self.assertEqual(self.supervisor.journal.db.execute("SELECT state FROM cancellations").fetchone()[0], "confirmed")
        self.assertEqual(self.adapter.sent, [])

    def test_unconfirmed_cancel_blocks_emergency_submission(self):
        journal = OrderJournal(self.directory, self.binding)
        entry = order("pending", status="open", filled=0, amount=1)
        self.seed(journal, "pending", entry, settled=False)
        journal.close()
        self.adapter.orders["pending"] = entry
        self.adapter.cancel_entry = Mock(side_effect=UnverifiedAccount("Timeout"))
        for _ in range(2):
            self.assertFalse(self.supervisor.step())
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(self.supervisor.journal.db.execute("SELECT state FROM cancellations").fetchone()[0], "uncertain")

    def test_protective_reduce_only_order_preserved(self):
        self.adapter.orders["protective"] = order("protective", side="sell", reduce=True, filled=0, status="open")
        self.supervisor.step()
        self.assertTrue(self.supervisor.step())
        self.assertEqual(self.adapter.canceled, [])
        self.assertEqual(self.adapter.orders["protective"]["status"], "open")

    def test_existing_unresolved_intent_blocks_liquidation_and_confirmation(self):
        journal = OrderJournal(self.directory, self.binding)
        self.seed(journal, "unknown", order("unknown", filled=0, status="open"), settled=False)
        journal.close()
        self.assertFalse(self.supervisor.step())
        self.assertEqual(self.adapter.sent, [])

    def test_exchange_outage_keeps_halt_visible(self):
        self.adapter.snapshot = Mock(side_effect=UnverifiedAccount("Bybit offline"))
        self.assertFalse(self.supervisor.step())
        self.assertFalse(self.state()["confirmed"])
        self.assertEqual(self.state()["detail"], "Bybit offline")
        self.assertEqual(self.adapter.sent, [])

    def test_change_during_flat_confirmation_is_not_certified(self):
        self.supervisor.step()
        original = self.adapter.snapshot
        calls = [0]
        def changed():
            calls[0] += 1
            if calls[0] == 3:
                self.adapter.amount = 1
            return original()
        self.adapter.snapshot = changed
        self.assertFalse(self.supervisor.step())
        self.assertFalse(self.state()["confirmed"])
        self.assertEqual(len(self.adapter.sent), 1)

    def test_short_position_is_closed_by_reduce_only_buy(self):
        journal = OrderJournal(self.directory, self.binding)
        with journal.db:
            journal.db.execute("DELETE FROM intents")
        self.seed(journal, "short", order("short", side="sell"))
        journal.close()
        self.adapter.amount = -2
        self.supervisor.step()
        self.assertEqual(self.adapter.sent[0][1]["side"], "buy")
        self.assertTrue(self.supervisor.step())

    def test_binding_mismatch_rejected(self):
        self.supervisor.close()
        with self.assertRaises(JournalBlocked):
            EmergencySupervisor(self.adapter, self.directory, self.state_file,
                                {**self.profile, "account_id": "999"}, KEY)

    def test_independent_guard_reads_equity_and_halts_without_rpc(self):
        control = Mock()
        control.get_equity.side_effect = AssertionError("Must not use Freqtrade balance")
        control.get_prop_profile.side_effect = AssertionError("Must not depend on Freqtrade profile RPC")
        control.force_exit.side_effect = AssertionError("Must not use Freqtrade force-exit")
        control.get_open_trades.side_effect = AssertionError("Must not depend on ORM or status")
        facade = IndependentRiskAPI(self.adapter, control)
        state = reconciled_state(self.profile, baseline(self.profile, now=self.now), self.now, halted=False)
        self.state_file.write_text(json.dumps(state))
        guard = RiskGuard(facade, self.state_file, profile=self.profile, emergency=self.supervisor)
        self.assertTrue(guard._check_drawdown(self.now))
        self.assertTrue(guard.nuclear_triggered)
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertTrue(guard._check_drawdown(self.now))
        self.assertTrue(guard.state["liquidation_confirmed"])
        control.get_open_trades.assert_not_called()
        control.force_exit.assert_not_called()

    def test_missing_equity_is_fault_not_numeric_breach(self):
        self.adapter.equity_value = None
        state = reconciled_state(self.profile, baseline(self.profile, now=self.now), self.now, halted=False)
        self.state_file.write_text(json.dumps(state))
        guard = RiskGuard(IndependentRiskAPI(self.adapter, Mock()), self.state_file,
                          profile=self.profile, emergency=self.supervisor)
        self.assertFalse(guard._check_drawdown(self.now))
        self.assertFalse(guard.nuclear_triggered)
        self.assertEqual(self.adapter.sent, [])

    def test_unverified_account_during_profile_check_is_fault_not_permanent_state_error(self):
        self.adapter.verify_identity = Mock(side_effect=UnverifiedAccount("Bybit request failed"))
        state = reconciled_state(self.profile, baseline(self.profile, now=self.now), self.now, halted=False)
        self.state_file.write_text(json.dumps(state))
        guard = RiskGuard(IndependentRiskAPI(self.adapter, Mock()), self.state_file,
                          profile=self.profile, emergency=self.supervisor)
        self.assertFalse(guard._check_drawdown(self.now))
        self.assertIsNone(guard.state_error)
        self.assertTrue(guard.state["data_fault"])
        self.assertFalse(guard.nuclear_triggered)

    def test_corrupt_or_missing_state_file_still_persists_halt_and_liquidates_on_overall_floor_breach(self):
        self.state_file.write_text("{corrupt-state-json")
        self.adapter.equity_value = 4740  # Operating overall floor on 5000 balance
        facade = IndependentRiskAPI(self.adapter, Mock())
        guard = RiskGuard(facade, self.state_file, profile=self.profile, emergency=self.supervisor)
        self.assertIsNotNone(guard.state_error)
        self.assertTrue(guard._check_drawdown(self.now))
        self.assertTrue(guard.nuclear_triggered)
        self.assertEqual(self.state_file.with_suffix(".corrupt.json").read_text(), "{corrupt-state-json")
        persisted = json.loads(self.state_file.read_text())
        self.assertTrue(persisted["nuclear_triggered"])
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertTrue(guard._check_drawdown(self.now))
        self.assertTrue(guard.state["liquidation_confirmed"])
        self.assertTrue(json.loads(self.state_file.read_text())["liquidation_confirmed"])


class AdapterTests(unittest.TestCase):
    def setUp(self):
        net = patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden"))
        net.start()
        self.addCleanup(net.stop)
        import ccxt
        self.api = ccxt.bybit({"apiKey": KEY, "secret": "offline-fake-secret"})
        self.api.enable_demo_trading(True)
        config = configured_account()
        config["prop_guard"]["account_id"] = "123"
        self.profile = profile_from_config(config)
        self.adapter = SupervisorBybit(self.api, self.profile)
        market = {"id": "BTCUSDT", "symbol": PAIR, "base": "BTC", "quote": "USDT", "settle": "USDT",
                  "type": "swap", "swap": True, "spot": False, "linear": True, "inverse": False,
                  "contract": True, "option": False, "future": False, "contractSize": 10.,
                  "precision": {"amount": .1, "price": .1}, "limits": {"amount": {"min": .1}, "cost": {"min": None}},
                  "info": {"lotSizeFilter": {"maxMktOrderQty": "100"}}}
        self.api.set_markets([market])
        self.api.privateGetV5UserQueryApi = Mock(return_value=self.response({"userID": "123", "readOnly": 0}))
        self.api.privateGetV5AccountInfo = Mock(return_value=self.response({"unifiedMarginStatus": 5, "marginMode": "ISOLATED_MARGIN"}))
        self.api.privateGetV5AccountWalletBalance = Mock(return_value=self.response({"list": [{"accountType": "UNIFIED", "coin": [
            {"coin": "USDT", "equity": "4820", "walletBalance": "5000", "borrowAmount": "0"}]}]}))
        self.api.privateGetV5OrderRealtime = Mock(return_value=self.page([]))
        self.api.privateGetV5OrderHistory = Mock(return_value=self.page([]))
        self.api.privateGetV5ExecutionList = Mock(return_value=self.page([]))
        self.api.privateGetV5PositionList = Mock(return_value=self.page([]))
        for endpoint in (self.api.privateGetV5PositionList, self.api.privateGetV5OrderRealtime):
            endpoint.side_effect = lambda params, endpoint=endpoint: (
                self.response(dict(category=params["category"], list=[], nextPageCursor=""))
                if params["category"] != "linear" or params.get("settleCoin") == "USDC"
                else endpoint.return_value)

    def response(self, result): return dict(retCode=0, time=int(time.time()*1000), result=result)
    def page(self, rows, cursor=""): return self.response(dict(category="linear", list=rows, nextPageCursor=cursor))

    def test_remote_uid_and_demo_endpoint_verified_before_equity(self):
        self.assertEqual(self.adapter.verify_identity(), self.profile)
        self.assertEqual(self.adapter.equity(), 4820)
        self.api.privateGetV5UserQueryApi.assert_called_once()

    def test_wrong_uid_rejected(self):
        self.api.privateGetV5UserQueryApi.return_value = self.response({"userID": "999", "readOnly": 0})
        with self.assertRaises(AccountMismatch): self.adapter.verify_identity()

    def test_wrong_environment_rejected_before_request(self):
        self.api.enable_demo_trading(False)
        with self.assertRaises(AccountMismatch): self.adapter.verify_identity()
        self.api.privateGetV5UserQueryApi.assert_not_called()

    def test_stale_bybit_response_rejected(self):
        self.api.privateGetV5UserQueryApi.return_value["time"] -= 16000
        with self.assertRaises(UnverifiedAccount): self.adapter.verify_identity()

    def test_late_response_is_rejected_under_overall_deadline(self):
        clock = [0.0]
        def delayed(params):
            clock[0] += 5
            return self.response({"userID": "123", "readOnly": 0})
        self.api.privateGetV5UserQueryApi.side_effect = delayed
        with patch("freqtrade.supervisor_bybit.time.monotonic", side_effect=lambda: clock[0]):
            with self.adapter.operation_budget(4):
                with self.assertRaises(UnverifiedAccount): self.adapter.verify_identity()
        self.api.privateGetV5AccountInfo.assert_not_called()

    def test_missing_uid_or_unsupported_mode_rejected(self):
        self.api.privateGetV5UserQueryApi.return_value = self.response({"readOnly": 0})
        with self.assertRaises(AccountMismatch): self.adapter.verify_identity()
        self.api.privateGetV5UserQueryApi.return_value = self.response({"userID": "123", "readOnly": 0})
        self.api.privateGetV5AccountInfo.return_value = self.response({"unifiedMarginStatus": 5, "marginMode": "REGULAR_MARGIN"})
        with self.assertRaises(AccountMismatch): self.adapter.verify_identity()

    def test_classic_mode_uses_contract_wallet(self):
        self.api.privateGetV5AccountInfo.return_value = self.response({"unifiedMarginStatus": 1})
        self.adapter.verify_identity()
        self.api.privateGetV5AccountWalletBalance.return_value["result"]["list"][0]["accountType"] = "CONTRACT"
        self.assertEqual(self.adapter.equity(), 4820)
        self.api.privateGetV5AccountWalletBalance.assert_called_with({"accountType": "CONTRACT"})

    def test_other_collateral_is_not_ignored(self):
        self.adapter.verify_identity()
        self.api.privateGetV5AccountWalletBalance.return_value["result"]["list"][0]["coin"].append(
            {"coin": "BTC", "equity": ".1", "walletBalance": ".1", "borrowAmount": "0"})
        with self.assertRaises(UnverifiedAccount): self.adapter.equity()

    def test_contract_size_and_position_direction(self):
        self.api.privateGetV5PositionList.return_value = self.page([
            dict(symbol="BTCUSDT", size="2", positionIdx=0, side="Sell", markPrice="100")])
        snap = self.adapter.snapshot()
        self.assertEqual(snap["positions"][0]["amount"], 20)
        self.assertEqual(snap["positions"][0]["side"], "sell")

    def test_hedged_position_is_rejected(self):
        self.api.privateGetV5PositionList.return_value = self.page([
            dict(symbol="BTCUSDT", size="2", positionIdx=1, side="Buy", markPrice="100")])
        with self.assertRaises(UnverifiedAccount): self.adapter.snapshot()

    def test_rounding_minimum_and_reduce_only_submission(self):
        position = dict(pair=PAIR, side="sell", contracts=1.25, amount=12.5, mark_price=100)
        request = self.adapter.close_request(position)
        self.assertEqual(request["amount"], 12)
        self.assertEqual(request["side"], "buy")
        self.api.create_order = Mock(return_value={"id": "exit", "clientOrderId": "stable"})
        self.adapter.create_order(**request, client_order_id="stable")
        self.assertEqual(self.api.create_order.call_args.args[3], 1.2)
        self.assertEqual(self.api.create_order.call_args.args[-1], {"reduceOnly": True, "positionIdx": 0, "clientOrderId": "stable"})
        with self.assertRaises(UnverifiedAccount): self.adapter.close_request({**position, "contracts": .01})

    def test_adapter_cannot_send_entry_or_cancel_reduce_only(self):
        with self.assertRaises(UnverifiedAccount):
            self.adapter.create_order(pair=PAIR, side="buy", amount=2, ordertype="limit", reduceOnly=False, client_order_id="x")
        with self.assertRaises(UnverifiedAccount): self.adapter.cancel_entry({"reduceOnly": True})

    def test_exception_text_never_exposes_credentials(self):
        self.api.privateGetV5UserQueryApi.side_effect = Exception("secret=DO_NOT_LOG")
        with self.assertRaises(UnverifiedAccount) as caught: self.adapter.verify_identity()
        self.assertNotIn("DO_NOT_LOG", str(caught.exception))

    def test_incomplete_pagination_is_not_flat(self):
        self.api.privateGetV5OrderRealtime.return_value = self.response({"category": "linear", "list": []})
        with self.assertRaises(KeyError): self.adapter.snapshot()
        self.api.privateGetV5OrderRealtime.return_value = self.response(None)
        with self.assertRaises(UnverifiedAccount): self.adapter.snapshot()

    def test_out_of_scope_open_order_prevents_account_flat_claim(self):
        self.api.privateGetV5OrderRealtime.side_effect = lambda p: self.response(
            dict(category=p["category"], list=[{"id": "external"}], nextPageCursor=""))
        with self.assertRaises(UnverifiedAccount): self.adapter.snapshot()

    def test_lookup_checks_closed_orders_and_actual_execution_totals(self):
        raw = dict(orderId="exit", orderLinkId="cid", symbol="BTCUSDT", side="Sell", orderType="Market",
                   orderStatus="Filled", qty="2", cumExecQty="2", leavesQty="0", avgPrice="100", price="100",
                   reduceOnly=True, positionIdx=0, createdTime="1700000000000", updatedTime="1700000000001")
        self.api.privateGetV5OrderHistory.return_value = self.page([raw])
        self.api.privateGetV5ExecutionList.return_value = self.page([
            dict(execId="fill", orderId="exit", orderLinkId="cid", symbol="BTCUSDT", side="Sell", execQty="2")])
        result = self.adapter.lookup(PAIR, client_id="cid")
        self.assertEqual(result["filled"], 20)
        self.assertEqual(result["status"], "closed")
        self.api.privateGetV5ExecutionList.return_value["result"]["list"][0]["execQty"] = "1"
        with self.assertRaises(UnverifiedAccount): self.adapter.lookup(PAIR, client_id="cid")

    def test_transient_read_error_retries_within_deadline_while_create_order_never_retries(self):
        valid = self.response({"userID": "123", "readOnly": 0})
        self.api.privateGetV5UserQueryApi.side_effect = [Exception("transient glitch"), valid]
        self.assertEqual(self.adapter.verify_identity(), self.profile)
        self.assertEqual(self.api.privateGetV5UserQueryApi.call_count, 2)
        self.api.create_order = Mock(side_effect=Exception("lost reply"))
        with self.assertRaises(UnverifiedAccount):
            self.adapter.create_order(
                pair=PAIR, side="buy", amount=10, ordertype="market", reduceOnly=True, client_order_id="cid1"
            )
        self.api.create_order.assert_called_once()

    def test_certify_flat_midnight_baseline_succeeds_only_when_flat_and_zero_intraday_activity(self):
        now = datetime(2026, 11, 5, 8, 30, tzinfo=UTC)
        certified = self.adapter.certify_flat_midnight_baseline(now)
        self.assertEqual(certified["source"], "bybit_v5_flat_account_verified")
        self.assertEqual(certified["equity"], 4820.0)
        self.assertTrue(certified["confirmed"])
        # If intraday execution exists, must fail closed
        self.api.privateGetV5ExecutionList.return_value = self.page([{"execId": "e1"}])
        with self.assertRaises(UnverifiedAccount):
            self.adapter.certify_flat_midnight_baseline(now)


if __name__ == "__main__":
    unittest.main()
