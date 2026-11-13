"""Regression tests for 24/7 autonomous AWS/Bybit HyroTrader operation."""

import hashlib
import json
import logging
import os
import socket
import sys
import tempfile
import time
import unittest
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from freqtrade.commands.trade_commands import start_trading
from freqtrade.emergency_authority import EmergencyTakeover
from freqtrade.freqtradebot import FreqtradeBot
from freqtrade.order_journal import OrderJournal
from freqtrade.prop_capital import prop_capital_available
from freqtrade.prop_controls import read_control, sync_prop_controls
from freqtrade.prop_profile import profile_from_config, validate_baseline
from freqtrade.prop_risk_approval import reviewed_policy
from freqtrade.prop_security import supervisor_config, validate_deployment
from freqtrade.supervisor_bybit import AccountMismatch, SupervisorBybit, UnverifiedAccount
from llm_risk_manager_prop import BinanceMacroMonitor, RiskManager
from prop_approve_config import approve_and_bind_config, build_flat_initial_baseline
from prop_independent import IndependentRiskAPI
from prop_risk_guard import RiskGuard, finite_number, market_crash, reconciled_state
from test_prop_profile import baseline, configured_account


def _resp(payload, status_code=200):
    r = Mock(status_code=status_code)
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


class AutonomousMacroMonitorTests(unittest.TestCase):
    def setUp(self):
        patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")).start()
        self.addCleanup(patch.stopall)

    def test_bybit_primary_newest_first_bars_and_thresholds(self):
        monitor = BinanceMacroMonitor()
        now_ms = int(time.time() * 1000)
        # Bybit returns descending timestamps; oldest close=100k, latest close=95.5k (-4.5%)
        bars = [[str(now_ms - i * 60000), "0", "0", "0", "95500.0" if i == 0 else "100000.0"] for i in range(61)]

        def fake_get(url, **kwargs):
            if "api.bybit.com/v5/market/tickers" in url and "BTCUSDT" in url:
                return _resp({"retCode": 0, "time": now_ms, "result": {"list": [{"lastPrice": "95500", "price24hPcnt": "-0.085"}]}})
            if "api.bybit.com/v5/market/tickers" in url and "ETHUSDT" in url:
                return _resp({"retCode": 0, "time": now_ms, "result": {"list": [{"price24hPcnt": "-0.092"}]}})
            if "api.bybit.com/v5/market/kline" in url:
                return _resp({"retCode": 0, "result": {"list": bars}})
            raise AssertionError(f"Unexpected fallback call when Bybit succeeded: {url}")

        with patch("llm_risk_manager_prop.requests.get", side_effect=fake_get):
            m = monitor.get_metrics()
        self.assertAlmostEqual(m["btc_1h_pct"], -4.5, places=4)
        self.assertAlmostEqual(m["btc_24h_pct"], -8.5, places=4)
        self.assertAlmostEqual(m["eth_24h_pct"], -9.2, places=2)
        self.assertTrue(m["is_flash_crash"])
        self.assertTrue(m["is_heavy_bear"])

    def test_multi_venue_fallback_to_okx_when_bybit_and_binance_fail(self):
        monitor = BinanceMacroMonitor()
        now_ms = int(time.time() * 1000)
        okx_bars = [[str(now_ms - i * 60000), "0", "0", "0", "101000.0" if i == 0 else "100000.0"] for i in range(61)]

        def fake_get(url, **kwargs):
            if "okx.com/api/v5/market/ticker" in url:
                return _resp({"code": "0", "data": [{"ts": str(now_ms), "last": "101000", "open24h": "100000"}]})
            if "okx.com/api/v5/market/candles" in url:
                return _resp({"code": "0", "data": okx_bars})
            raise ConnectionError("geo-blocked / timeout")

        with patch("llm_risk_manager_prop.requests.get", side_effect=fake_get):
            m = monitor.get_metrics()
        self.assertAlmostEqual(m["btc_1h_pct"], 1.0, places=4)
        self.assertAlmostEqual(m["btc_24h_pct"], 1.0, places=4)
        self.assertFalse(m["is_flash_crash"])
        self.assertFalse(m["is_heavy_bear"])

    def test_macro_loop_requires_two_clean_cycles_for_hysteresis_resume(self):
        rm = RiskManager.__new__(RiskManager)
        rm._shutdown = SimpleNamespace(
            is_set=Mock(side_effect=[False, False, False, False, True]),
            wait=Mock(),
        )
        rm.request_macro_pause = Mock()
        rm.macro_monitor = SimpleNamespace(
            get_metrics=Mock(side_effect=[
                {"btc_1h_pct": -4.2, "btc_24h_pct": -3.0},   # crash -> pause
                {"btc_1h_pct": -2.8, "btc_24h_pct": -3.0},   # below -2.5% recovery band -> stay paused
                {"btc_1h_pct": -2.0, "btc_24h_pct": -3.0},   # clean #1 (< 2 needed)
                {"btc_1h_pct": -1.5, "btc_24h_pct": -3.0},   # clean #2 -> resume
            ])
        )
        rm._macro_loop()
        self.assertEqual(
            [c.args[0] for c in rm.request_macro_pause.call_args_list],
            [True, False],
        )


class AutonomousRolloverAndTransientTests(unittest.TestCase):
    def setUp(self):
        patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")).start()
        self.addCleanup(patch.stopall)
        log_factory = logging.getLogRecordFactory()
        self.addCleanup(logging.setLogRecordFactory, log_factory)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = configured_account()
        self.config["user_data_dir"] = self.root
        self.profile = profile_from_config(self.config)
        self.state_file = self.root / "risk_manager_prop_state.json"

    def _bybit_adapter(self, midnight_dt, *, equity=5060.0, wallet=5000.0, tx_rows=None, exec_rows=None, mark_mid=102.0, clock=None):
        api = Mock()
        api.id = "bybit"
        api.options = {"sandbox": True}
        api.urls = {"api": {"private": "https://api-demo.bybit.com"}}
        api.implode_hostname = lambda url: url
        api.enable_demo_trading = lambda flag: api.options.__setitem__("sandbox", bool(flag))
        api.markets = {
            "SOLUSDT": {"symbol": "SOL/USDT:USDT", "id": "SOLUSDT", "contractSize": 1.0,
                        "swap": True, "linear": True, "settle": "USDT",
                        "limits": {"amount": {"min": 0.1}, "cost": {"min": 5.0}}}
        }
        api.market = lambda pair: api.markets["SOLUSDT"]
        default_ms = int((midnight_dt + timedelta(seconds=3)).timestamp() * 1000)
        mid_ms = int(midnight_dt.timestamp() * 1000)
        ts = lambda: int(clock[0].timestamp() * 1000) if clock is not None else default_ms

        api.privateGetV5UserQueryApi.side_effect = lambda *_a, **_kw: {"retCode": 0, "time": ts(), "result": {"userID": self.profile["account_id"], "readOnly": 0}}
        api.privateGetV5AccountInfo.side_effect = lambda *_a, **_kw: {"retCode": 0, "time": ts(), "result": {"unifiedMarginStatus": 3, "marginMode": "ISOLATED_MARGIN"}}
        api.privateGetV5AccountWalletBalance.side_effect = lambda *_a, **_kw: {
            "retCode": 0, "time": ts(),
            "result": {"list": [{"accountType": "UNIFIED", "coin": [
                {"coin": "USDT", "equity": str(equity), "walletBalance": str(wallet),
                 "borrowAmount": "0", "unrealisedPnl": str(equity - wallet)}
            ]}]}
        }

        def order_realtime(params):
            return {"retCode": 0, "time": ts(), "result": {"category": params.get("category", "linear"), "list": [], "nextPageCursor": ""}}

        def pos_list(params):
            if params.get("category") != "linear" or params.get("settleCoin") != "USDT":
                return {"retCode": 0, "time": ts(), "result": {"category": params.get("category", "linear"), "list": [], "nextPageCursor": ""}}
            return {
                "retCode": 0, "time": ts(),
                "result": {"category": "linear", "nextPageCursor": "", "list": [{
                    "symbol": "SOLUSDT", "size": "3", "positionIdx": 0, "tradeMode": 1,
                    "side": "Buy", "markPrice": "120.0", "unrealisedPnl": str(equity - wallet), "avgPrice": "100.0",
                }]},
            }

        api.privateGetV5OrderRealtime.side_effect = order_realtime
        api.privateGetV5PositionList.side_effect = pos_list
        api.privateGetV5AccountTransactionLog.side_effect = lambda *_a, **_kw: {
            "retCode": 0, "time": ts(),
            "result": {"list": tx_rows if tx_rows is not None else [], "nextPageCursor": ""},
        }
        api.privateGetV5ExecutionList.side_effect = lambda *_a, **_kw: {
            "retCode": 0, "time": ts(),
            "result": {"category": "linear", "list": exec_rows if exec_rows is not None else [], "nextPageCursor": ""},
        }
        api.publicGetV5MarketMarkPriceKline.side_effect = lambda *_a, **_kw: {
            "retCode": 0, "time": ts(),
            "result": {"category": "linear", "symbol": "SOLUSDT", "list": [[str(mid_ms), str(mark_mid), str(mark_mid), str(mark_mid), str(mark_mid)]]},
        }
        return SupervisorBybit(api, self.profile)

    def test_autonomous_rollover_reconstructs_midnight_equity_with_open_overnight_positions(self):
        day1 = datetime(2026, 11, 5, 23, 59, 58, tzinfo=UTC)
        day2_mid = datetime(2026, 11, 6, 0, 0, 0, tzinfo=UTC)
        day2_now = day2_mid + timedelta(seconds=3)
        self.state_file.write_text(json.dumps(reconciled_state(self.profile, baseline(self.profile, now=day1), day1, halted=False)))

        # Funding -0.50 USDT settled at 00:00:01 -> midnight wallet = 5000 - (-0.50) = 5000.50
        # Open long 3 SOL @ mark=120 now (upl=+60), midnight mark=110 -> midnight upl = 60 - 3*(120-110) = +30
        # Expected midnight equity = 5000.50 + 30.00 = 5030.50
        txs = [{"type": "SETTLEMENT", "transactionTime": str(int((day2_mid + timedelta(seconds=1)).timestamp() * 1000)), "change": "-0.5"}]
        direct = self._bybit_adapter(day2_mid, equity=5060.0, wallet=5000.0, tx_rows=txs, mark_mid=110.0)
        control = Mock()
        control.get_prop_control.return_value = {"verified": True, "flat": False, "pending_entries": False, "ack": None}
        api = IndependentRiskAPI(direct, control)
        guard = RiskGuard(api, self.state_file, profile=self.profile, autonomous_rollover=True, transient_grace_cycles=3)

        with patch("freqtrade.supervisor_bybit.time.time", return_value=day2_now.timestamp()):
            self.assertFalse(guard._check_drawdown(day2_now))
        self.assertTrue(guard.state["daily_monitoring_available"])
        self.assertFalse(guard.state["data_fault"])
        self.assertFalse(guard.state["wake_up_lock"])
        self.assertAlmostEqual(guard.state["baseline"]["equity"], 5030.5, places=4)
        self.assertEqual(guard.state["baseline"]["source"], "bybit_v5_reconciled_midnight_equity")
        self.assertEqual(len(guard.state["baseline"]["evidence"]["positions_midnight"]), 1)
        # Overnight open positions must NEVER trigger emergency liquidation when healthy
        control.force_exit.assert_not_called()

    def test_rollover_rejects_external_transfer_in_transaction_log(self):
        day2_mid = datetime(2026, 11, 6, 0, 0, 0, tzinfo=UTC)
        day2_now = day2_mid + timedelta(seconds=3)
        txs = [{"type": "TRANSFER_IN", "transactionTime": str(int(day2_mid.timestamp() * 1000)), "change": "100"}]
        direct = self._bybit_adapter(day2_mid, equity=5060.0, wallet=5000.0, tx_rows=txs, mark_mid=110.0)
        with patch("freqtrade.supervisor_bybit.time.time", return_value=day2_now.timestamp()):
            with self.assertRaisesRegex(UnverifiedAccount, "External or unverified capital flow"):
                direct.reconcile_midnight_baseline(day2_now)

    def test_transient_equity_glitch_pauses_then_auto_recovers_after_three_clean_cycles(self):
        now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
        b = baseline(self.profile, now=now)
        (self.root / "risk_manager_prop_baseline.json").write_text(json.dumps(b))
        self.state_file.write_text(json.dumps(reconciled_state(self.profile, b, now, halted=False)))

        api = Mock()
        api.operation_budget = None
        api.deadline_expired.return_value = False
        api.get_prop_profile.return_value = self.profile
        api.get_prop_control.return_value = {"verified": True, "flat": True, "pending_entries": False, "ack": None}
        api.stop_buying.return_value = True
        api.start_trading.return_value = True

        guard = RiskGuard(api, self.state_file, profile=self.profile, transient_grace_cycles=3, max_transient_outage_cycles=12)
        # Healthy initial tick
        api.get_equity.return_value = 5000.0
        guard._check_drawdown(now)
        self.assertFalse(guard.state["data_fault"])
        self.assertFalse(guard.state["wake_up_lock"])

        # 2 transient API failures pause reversibly without sticky data_fault
        api.get_equity.return_value = None
        guard._check_drawdown(now + timedelta(seconds=5))
        guard._check_drawdown(now + timedelta(seconds=10))
        self.assertFalse(guard.state["data_fault"])
        self.assertTrue(guard.state["transient_paused"])
        self.assertTrue(guard.state["wake_up_lock"])
        self.assertFalse(prop_capital_available(self.root, now + timedelta(seconds=10), self.config))

        # 3 clean cycles auto-clear transient pause & resume trading
        api.get_equity.return_value = 5000.0
        guard._check_drawdown(now + timedelta(seconds=15))
        self.assertTrue(guard.state["transient_paused"])
        guard._check_drawdown(now + timedelta(seconds=20))
        self.assertTrue(guard.state["transient_paused"])
        guard._check_drawdown(now + timedelta(seconds=25))
        self.assertFalse(guard.state["transient_paused"])
        self.assertFalse(guard.state["wake_up_lock"])
        self.assertFalse(guard.state["data_fault"])
        api.start_trading.assert_called_once()

    def test_sustained_outage_and_account_mismatch_latch_permanently(self):
        now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
        b = baseline(self.profile, now=now)
        (self.root / "risk_manager_prop_baseline.json").write_text(json.dumps(b))
        self.state_file.write_text(json.dumps(reconciled_state(self.profile, b, now, halted=False)))

        api = Mock()
        api.get_prop_profile.side_effect = AccountMismatch("UID changed")
        api.get_prop_control.return_value = {"verified": True, "flat": True, "pending_entries": False, "ack": None}
        api.stop_buying.return_value = True
        guard = RiskGuard(api, self.state_file, profile=self.profile, transient_grace_cycles=3)
        guard._check_drawdown(now)
        self.assertTrue(guard.state["data_fault"])
        self.assertTrue(guard.state["wake_up_lock"])


class HyroRulesAndCommissioningTests(unittest.TestCase):
    def setUp(self):
        patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")).start()
        self.addCleanup(patch.stopall)
        log_factory = logging.getLogRecordFactory()
        self.addCleanup(logging.setLogRecordFactory, log_factory)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_daily_consistency_cap_pauses_and_clears_next_day(self):
        now = datetime.now(UTC)
        today = now.strftime("%Y-%m-%d")
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

        rm = RiskManager.__new__(RiskManager)
        rm._shutdown = SimpleNamespace(is_set=Mock(side_effect=[False, False, True]), wait=Mock())
        rm.state = {"nuclear_triggered": False, "last_equity": 5000.0}
        rm.initial_balance = 5000.0
        rm.profile = {"profit_target_percent": 0.1, "daily_profit_cap_percent": 0.4, "min_trading_days": 5, "phase_start_utc": None}
        rm.request_data_fault = Mock()
        rm.request_rules_pause = Mock()
        rm.rules_api = SimpleNamespace(
            get_open_trades=Mock(return_value=[]),
            get_trades=Mock(side_effect=[
                [{"close_date": f"{today}T08:00:00+00:00", "profit_abs": 180.0, "cost": 333.0, "profit_ratio": 0.05}],
                [{"close_date": f"{yesterday}T08:00:00+00:00", "profit_abs": 180.0, "cost": 333.0, "profit_ratio": 0.05}],
            ]),
        )
        rm._hyrotrader_rules_loop()
        rm.request_data_fault.assert_not_called()
        self.assertEqual(rm.request_rules_pause.call_args_list[0].args[0], True)
        self.assertEqual(rm.request_rules_pause.call_args_list[1].args[0], False)

    def test_emergency_takeover_exits_cleanly_with_zero_in_trade_commands(self):
        worker = Mock()
        worker.run.side_effect = EmergencyTakeover("Emergency halt active")
        with patch("freqtrade.worker.Worker", return_value=worker):
            rc = start_trading({})
        self.assertEqual(rc, 0)
        worker.exit.assert_called_once()

    def test_prop_remaining_headroom_clamps_dca_and_entry(self):
        bot = FreqtradeBot.__new__(FreqtradeBot)
        bot.config = {
            "prop_guard_enabled": True,
            "prop_risk_budget": {"max_aggregate_notional_usdt": 1000.0, "max_slot_notional_usdt": 666.0},
        }
        bot.order_journal = SimpleNamespace(
            risk_snapshot={
                "positions": [{"symbol": "ETH/USDT:USDT", "amount": 3.0, "mark_price": 300.0}],  # $900 used
                "open_orders": [],
            }
        )
        rem = bot._prop_remaining_headroom("BTC/USDT:USDT")
        self.assertIsNotNone(rem)
        self.assertAlmostEqual(rem, 100.0 * 0.995, places=3)

    def test_approve_config_seals_both_live_and_demo_environments(self):
        base_cfg = json.loads((ROOT / "user_data" / "config_prop_nfix8.json").read_text())
        for env_name in ("demo", "live"):
            with self.subTest(env=env_name):
                approved = approve_and_bind_config(
                    base_cfg,
                    account_id="987654321",
                    challenge_id="HYRO-1STEP-42",
                    environment=env_name,
                    reference="audit-ticket-001",
                )
                profile = profile_from_config(approved)
                self.assertEqual(profile["environment"], env_name)
                req = {"leverage": 1.0, "ordertype": "limit", "time_in_force": "GTC"}
                self.assertTrue(reviewed_policy(profile, approved["prop_risk_budget"], approved, req, {}))

                # Verify full deployment gate passes with baseline & matching supervisor env
                d = self.root / f"d_{env_name}"
                d.mkdir()
                approved["user_data_dir"] = d
                b = build_flat_initial_baseline(profile, equity=5000.0, reference="audit-ticket-001")
                (d / "risk_manager_prop_baseline.json").write_text(json.dumps(b))
                (d / "risk_manager_prop_state.json").write_text(json.dumps({"profile": profile}))
                env = {
                    k: hashlib.sha256(k.encode()).hexdigest()
                    for k in ("PROP_BYBIT_API_KEY", "PROP_BYBIT_API_SECRET", "FREQTRADE_PASSWORD",
                              "FREQTRADE_JWT_TOKEN", "FREQTRADE_WS_TOKEN")
                }
                env.update(
                    FREQTRADE_USERNAME="op",
                    PROP_EMERGENCY_AUTHORITY="dedicated-account-single-host",
                    PROP_PRIVATE_DOCKER_NETWORK="prop_control",
                    FREQTRADE_API_URL="http://freqtrade_prop:8088/api/v1",
                )
                identity = {k: profile[k] for k in ("profile_id", "account_id", "challenge_id",
                                                    "phase_id", "exchange", "environment", "currency")}
                identity["key_fingerprint"] = hashlib.sha256(env["PROP_BYBIT_API_KEY"].encode()).hexdigest()
                identity["database"] = approved["db_url"]
                j = OrderJournal(d / "order_journal", identity)
                try:
                    sup_cfg = supervisor_config(approved, env)
                    sup_cfg["user_data_dir"] = d
                    with patch("freqtrade.prop_security.private_path"), patch("os.umask"), patch("os.geteuid", return_value=1000, create=True):
                        self.assertEqual(validate_deployment(sup_cfg, sup_cfg["exchange"], env), profile)
                finally:
                    j.close()

    def test_config_loads_bybit_pairlist_and_nfix8_strategy_cleanly(self):
        from freqtrade.configuration import Configuration
        from freqtrade.exchange.bybit import Bybit
        from freqtrade.plugins.pairlistmanager import PairListManager
        from freqtrade.resolvers import StrategyResolver

        cfg = Configuration.from_files([str(ROOT / "user_data" / "config_prop_nfix8.json")])
        with patch.object(Bybit, "validate_config", return_value=None), \
             patch.object(Bybit, "reload_markets", return_value=None), \
             patch.object(Bybit, "additional_exchange_init", return_value=None):
            ex = Bybit(cfg, validate=False)
        ex._markets = {
            "BTC/USDT:USDT": {"active": True, "quote": "USDT", "swap": True, "spot": False, "linear": True},
        }
        pm = PairListManager(ex, cfg)
        self.assertEqual(pm.name_list, ["VolumePairList", "DelistFilter", "AgeFilter", "SpreadFilter"])
        strat = StrategyResolver.load_strategy(cfg)
        self.assertEqual(strat.get_strategy_name(), "NostalgiaForInfinityX8_Prop")
        self.assertEqual(cfg["max_open_trades"], 3)
        self.assertEqual(cfg["order_time_in_force"], {"entry": "GTC", "exit": "GTC"})


class BlindAuditRegressionTests(unittest.TestCase):
    def setUp(self):
        patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")).start()
        self.addCleanup(patch.stopall)
        log_factory = logging.getLogRecordFactory()
        self.addCleanup(logging.setLogRecordFactory, log_factory)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = configured_account()
        self.config["user_data_dir"] = self.root
        self.config["prop_guard_enabled"] = True
        self.profile = profile_from_config(self.config)
        self.state_file = self.root / "risk_manager_prop_state.json"
        self.identity = {k: self.profile[k] for k in ("account_id", "challenge_id", "phase_id", "profile_id")}
        self.journal = OrderJournal(self.root / "order_journal", self.identity)
        self.addCleanup(lambda: self.journal.close())
        from snapshot_fixtures import timed_snapshot
        self.journal.reconcile(timed_snapshot(dict(complete=True, open_orders=[], orders={}, fills={}, positions=[])), {}, {})
        self.trade_model = SimpleNamespace(get_open_trades=lambda: [])
        self.order_model = SimpleNamespace(get_open_orders=lambda: [])
        self.bot = SimpleNamespace(config=self.config, order_journal=self.journal)

    def test_midnight_rollover_transient_sync_controls_auto_recovers(self):
        from freqtrade.prop_controls import control_status
        helper = AutonomousRolloverAndTransientTests()
        helper.profile = self.profile
        day1 = datetime(2026, 11, 5, 23, 59, 58, tzinfo=UTC)
        day2_mid = datetime(2026, 11, 6, 0, 0, 0, tzinfo=UTC)
        day2_now = day2_mid + timedelta(seconds=3)
        self.state_file.write_text(json.dumps(reconciled_state(self.profile, baseline(self.profile, now=day1), day1, halted=False)))

        clock = [day2_now]
        direct = helper._bybit_adapter(day2_mid, equity=5060.0, wallet=5000.0, mark_mid=110.0, clock=clock)
        mid_ms = int(day2_mid.timestamp() * 1000)
        # Simulate Bybit 00:00:01 UTC transient delay publishing the 00:00 1m mark kline, then healthy recovery.
        kline_calls = [0]
        def dynamic_kline(*_a, **_kw):
            kline_calls[0] += 1
            now_ms = int(clock[0].timestamp() * 1000)
            bars = [] if kline_calls[0] == 1 else [[str(mid_ms), "110.0", "110.0", "110.0", "110.0"]]
            return {"retCode": 0, "time": now_ms, "result": {"category": "linear", "symbol": "SOLUSDT", "list": bars}}
        direct.api.publicGetV5MarketMarkPriceKline.side_effect = dynamic_kline
        control = Mock()
        control.operation_budget = None
        control.deadline_expired.return_value = False
        control.stop_buying.return_value = True
        control.start_trading.return_value = True
        dt_mock = SimpleNamespace(
            now=lambda tz=None: clock[0],
            fromtimestamp=datetime.fromtimestamp,
            fromisoformat=datetime.fromisoformat,
        )
        from snapshot_fixtures import timed_snapshot
        def live_prop_control():
            self.journal.reconcile(timed_snapshot(dict(complete=True, open_orders=[], orders={}, fills={}, positions=[])), {}, {})
            return control_status(self.bot, self.trade_model, self.order_model)

        control.get_prop_control.side_effect = live_prop_control
        api = IndependentRiskAPI(direct, control)
        guard = RiskGuard(api, self.state_file, profile=self.profile, autonomous_rollover=True, transient_grace_cycles=3)

        ts_fn = lambda: clock[0].timestamp()
        with patch("freqtrade.supervisor_bybit.time.time", side_effect=ts_fn), \
             patch("freqtrade.snapshot_freshness.time.time", side_effect=ts_fn), \
             patch("freqtrade.order_journal.time.time", side_effect=ts_fn), \
             patch("freqtrade.prop_controls.time.time", side_effect=ts_fn), \
             patch("snapshot_fixtures.time.time", side_effect=ts_fn), \
             patch("freqtrade.prop_controls.datetime", dt_mock):
            guard._check_drawdown(clock[0])
            self.assertTrue(guard.state["transient_paused"])
            self.assertFalse(guard.state["data_fault"])
            self.assertTrue(sync_prop_controls(self.bot))
            ctrl = read_control(self.journal)
            self.assertTrue(ctrl["macro_paused"])
            self.assertFalse(ctrl["data_fault"])

            # 3 clean cycles auto-recover without latching sticky data_fault
            for step in (5, 10, 15):
                clock[0] = day2_now + timedelta(seconds=step)
                guard._check_drawdown(clock[0])
            # Core's next process() tick reads cleared supervisor state and clears macro_paused
            self.assertFalse(sync_prop_controls(self.bot))
        self.assertFalse(guard.state["transient_paused"])
        self.assertFalse(guard.state["data_fault"])
        self.assertFalse(guard.state["wake_up_lock"])
        self.assertFalse(read_control(self.journal)["data_fault"])
        self.assertFalse(read_control(self.journal)["macro_paused"])
        control.start_trading.assert_called_once()

    def test_clean_shutdown_does_not_latch_sticky_fault_and_preserves_real_fault(self):
        now = datetime.now(UTC)
        b = baseline(self.profile, now=now)
        (self.root / "risk_manager_prop_baseline.json").write_text(json.dumps(b))
        self.state_file.write_text(json.dumps(reconciled_state(self.profile, b, now, halted=False)))

        api = Mock()
        api.operation_budget = None
        api.deadline_expired.return_value = False
        api.get_prop_profile.return_value = self.profile
        api.get_equity.return_value = 5000.0
        api.stop_buying.return_value = True
        api.start_trading.return_value = True
        api.get_prop_control.side_effect = lambda: {
            "verified": True, "flat": True, "pending_entries": False, "ack": None,
            **{k: v for k, v in read_control(self.journal).items() if k in ("data_fault", "risk_halt", "manual_lock")},
        }

        guard = RiskGuard(api, self.state_file, profile=self.profile, autonomous_rollover=True, transient_grace_cycles=2)
        guard._shutdown.wait = lambda _interval: guard._handle_shutdown(15, None)
        guard.run()

        self.assertFalse(guard.state["data_fault"])
        self.assertTrue(guard.state["transient_paused"])
        self.assertTrue(sync_prop_controls(self.bot))
        self.assertFalse(read_control(self.journal)["data_fault"])
        self.assertTrue(read_control(self.journal)["macro_paused"])
        self.assertEqual(self.journal.db.execute("SELECT count(*) FROM fault_events WHERE acknowledged=0").fetchone()[0], 0)

        # Restarted RiskGuard resumes cleanly after 2 cycles
        guard2 = RiskGuard(api, self.state_file, profile=self.profile, autonomous_rollover=True, transient_grace_cycles=2)
        guard2._check_drawdown(datetime.now(UTC))
        guard2._check_drawdown(datetime.now(UTC) + timedelta(seconds=5))
        self.assertFalse(guard2.state["transient_paused"])
        self.assertFalse(guard2.state["data_fault"])
        api.start_trading.assert_called_once()

        # Real existing data_fault survives _on_clean_shutdown intact
        guard2.state["data_fault"] = True
        guard2._on_clean_shutdown()
        self.assertTrue(guard2.state["data_fault"])

    def test_control_status_outage_monotonic_failure_and_startup_grace(self):
        now = datetime.now(UTC)
        b = baseline(self.profile, now=now)
        (self.root / "risk_manager_prop_baseline.json").write_text(json.dumps(b))
        self.state_file.write_text(json.dumps(reconciled_state(self.profile, b, now, halted=False)))

        api = Mock()
        api.operation_budget = None
        api.deadline_expired.return_value = False
        api.get_prop_profile.return_value = self.profile
        api.get_equity.return_value = 5000.0
        api.get_prop_control.return_value = None  # Core container still booting
        api.stop_buying.return_value = True
        api.start_trading.return_value = True

        guard = RiskGuard(
            api, self.state_file, profile=self.profile,
            transient_grace_cycles=2, max_transient_outage_cycles=3, startup_grace_cycles=5,
        )
        # 4 cycles waiting on initial boot < startup_grace_cycles=5 must NOT latch fault even > max_transient=3
        for i in range(4):
            guard._check_drawdown(now + timedelta(seconds=i * 5))
        self.assertEqual(guard._transient_failures, 4)
        self.assertFalse(guard.state["data_fault"])
        self.assertTrue(guard.state["transient_paused"])

        # Core comes online -> 2 clean cycles recover cleanly
        api.get_prop_control.return_value = {"verified": True, "flat": True, "pending_entries": False, "ack": None}
        guard._check_drawdown(now + timedelta(seconds=20))
        guard._check_drawdown(now + timedelta(seconds=25))
        self.assertTrue(guard._control_ever_seen)
        self.assertFalse(guard.state["transient_paused"])
        self.assertFalse(guard.state["data_fault"])

        # Once _control_ever_seen=True, a subsequent outage escalates monotonically after max_transient_outage_cycles=3
        api.get_prop_control.return_value = None
        for i in range(4):
            guard._check_drawdown(now + timedelta(seconds=30 + i * 5))
        self.assertTrue(guard.state["data_fault"])

    def test_compose_startup_order_and_secure_start_standalone_cli(self):
        import subprocess
        import yaml

        compose = yaml.safe_load((ROOT / "docker-compose.prop.yml").read_text())
        core = compose["services"]["freqtrade_prop"]
        sup = compose["services"]["freqtrade_prop_risk_manager"]
        self.assertEqual(core["depends_on"], {"freqtrade_prop_risk_manager": {"condition": "service_healthy"}})
        self.assertNotIn("depends_on", sup)
        sup_env = sup["environment"]
        self.assertEqual(sup_env.get("PROP_TELEGRAM_BOT_TOKEN"), "${PROP_TELEGRAM_BOT_TOKEN:-}")
        self.assertEqual(sup_env.get("PROP_TELEGRAM_CHAT_ID"), "${PROP_TELEGRAM_CHAT_ID:-}")
        self.assertEqual(sup_env.get("PROP_INACTIVITY_KEEPALIVE"), "${PROP_INACTIVITY_KEEPALIVE:-false}")

        proc = subprocess.run(
            [sys.executable, "-c", "import runpy, sys; sys.argv=['scripts/prop_secure_start.py']; runpy.run_path('scripts/prop_secure_start.py', run_name='__main__')"],
            cwd=str(ROOT),
            env={"PATH": os.environ.get("PATH", ""), "HOME": str(self.root)},
            capture_output=True,
            text=True,
        )
        # Expected rejection (missing args/creds), NEVER ModuleNotFoundError
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("ModuleNotFoundError", proc.stderr)
        self.assertIn("Deployment rejected", proc.stderr)

    def test_rollover_budget_and_mixed_trade_timestamp_parsing(self):
        from contextlib import contextmanager
        now = datetime.now(UTC)
        yesterday = now - timedelta(days=1)
        old_b = baseline(self.profile, now=yesterday)
        (self.root / "risk_manager_prop_baseline.json").write_text(json.dumps(old_b))
        self.state_file.write_text(json.dumps(reconciled_state(self.profile, old_b, yesterday, halted=False)))

        budgets = []
        api = Mock()
        @contextmanager
        def track_budget(sec=4.0):
            budgets.append(sec)
            yield
        api.operation_budget = track_budget
        api.deadline_expired.return_value = False
        api.get_prop_profile.return_value = self.profile
        api.get_equity.return_value = 5000.0
        api.reconcile_midnight_baseline.return_value = baseline(self.profile, now=now)
        api.get_prop_control.return_value = {"verified": True, "flat": True, "pending_entries": False, "ack": None}
        api.stop_buying.return_value = True
        api.start_trading.return_value = True

        guard = RiskGuard(api, self.state_file, profile=self.profile, autonomous_rollover=True, transient_grace_cycles=2)
        cycles = [0]
        def step_wait(_interval):
            cycles[0] += 1
            if cycles[0] >= 2:
                guard._handle_shutdown(15, None)
        guard._shutdown.wait = step_wait
        guard.run()
        self.assertEqual(budgets[:2], [12.0, 4.0])

        rm = RiskManager.__new__(RiskManager)
        rm.profile = self.profile
        rm.initial_balance = 5000.0
        rm.state = {}
        rm._shutdown = SimpleNamespace(is_set=Mock(side_effect=[False, True]), wait=Mock())
        rm.request_rules_pause = Mock()
        rm.rules_api = SimpleNamespace(
            get_open_trades=lambda: [],
            get_trades=lambda limit=500: [
                {"close_timestamp": int(now.timestamp() * 1000), "profit_abs": 20.0, "stake_amount": 333.0, "profit_ratio": 0.06},
                {"close_date": (now - timedelta(days=1)).isoformat(), "profit_abs": 15.0, "stake_amount": 300.0, "profit_ratio": 0.05},
            ],
        )
        rm._hyrotrader_rules_loop()
        rm.request_rules_pause.assert_called_once_with(False)


class PropAuditRegressionTests(unittest.TestCase):
    def setUp(self):
        patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")).start()
        self.addCleanup(patch.stopall)
        log_factory = logging.getLogRecordFactory()
        self.addCleanup(logging.setLogRecordFactory, log_factory)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.account_cfg = configured_account()
        self.profile = profile_from_config(self.account_cfg)
        self.state_file = self.root / "risk_manager_prop_state.json"

    def test_a2_midnight_rollover_grace_prevents_sticky_data_fault(self):
        day1 = datetime(2026, 11, 10, 23, 59, 57, tzinfo=UTC)
        day2_grace = datetime(2026, 11, 11, 0, 0, 2, tzinfo=UTC)
        day2_ready = datetime(2026, 11, 11, 0, 0, 5, tzinfo=UTC)
        day2_stale = datetime(2026, 11, 11, 0, 0, 25, tzinfo=UTC)

        b_day1 = baseline(self.profile, now=day1)
        b_day1["last_updated"] = day1.isoformat()
        state_day1 = reconciled_state(self.profile, b_day1, day1, halted=False)
        state_day1["data_valid"] = True
        state_day1["last_updated"] = day1.isoformat()
        self.state_file.write_text(json.dumps(state_day1))

        journal = OrderJournal(
            self.root,
            {
                "account_id": self.profile["account_id"],
                "challenge_id": self.profile["challenge_id"],
                "phase_id": self.profile["phase_id"],
                "profile_id": self.profile["profile_id"],
                "exchange": self.profile["exchange"],
                "environment": self.profile["environment"],
                "currency": self.profile["currency"],
                "key_fingerprint": hashlib.sha256(b"offline").hexdigest(),
            },
        )
        self.addCleanup(journal.close)
        bot = SimpleNamespace(
            config={**self.account_cfg, "prop_guard_enabled": True, "user_data_dir": str(self.root)},
            order_journal=journal,
        )

        # Within 15s grace right after 00:00 UTC: reversible rollover pause, NO sticky data_fault.
        with patch("freqtrade.prop_controls.datetime") as dt_mock:
            dt_mock.now.return_value = day2_grace
            dt_mock.fromisoformat = datetime.fromisoformat
            paused_grace = sync_prop_controls(bot)
        ctrl_grace = read_control(journal)
        self.assertTrue(paused_grace)
        self.assertFalse(ctrl_grace["data_fault"])
        self.assertTrue(ctrl_grace["macro_paused"])

        # Once supervisor publishes day2 baseline: automatic recovery without manual ACK.
        b_day2 = baseline(self.profile, now=day2_ready)
        state_day2 = reconciled_state(self.profile, b_day2, day2_ready, halted=False)
        state_day2["data_valid"] = True
        self.state_file.write_text(json.dumps(state_day2))
        with patch("freqtrade.prop_controls.datetime") as dt_mock:
            dt_mock.now.return_value = day2_ready
            dt_mock.fromisoformat = datetime.fromisoformat
            paused_ready = sync_prop_controls(bot)
        ctrl_ready = read_control(journal)
        self.assertFalse(paused_ready)
        self.assertFalse(ctrl_ready["data_fault"])
        self.assertFalse(ctrl_ready["macro_paused"])

        # Beyond 15s stale window without new baseline: still fails closed into data_fault.
        self.state_file.write_text(json.dumps(state_day1))
        with patch("freqtrade.prop_controls.datetime") as dt_mock:
            dt_mock.now.return_value = day2_stale
            dt_mock.fromisoformat = datetime.fromisoformat
            sync_prop_controls(bot)
        ctrl_stale = read_control(journal)
        self.assertTrue(ctrl_stale["data_fault"])

        # RiskGuard cross-midnight response_day_mismatch uses transient pause then self-recovers.
        (self.root / "risk_manager_prop_baseline.json").write_text(json.dumps(b_day1))
        self.state_file.write_text(json.dumps(state_day1))
        api = Mock()
        api.get_prop_profile.return_value = self.profile
        api.get_equity.return_value = 5000.0
        api.equity_observation = {
            "equity": 5000.0,
            "response_time_ms": int(day1.timestamp() * 1000),
            "source": "bybit_v5_wallet_response",
        }
        api.reconcile_midnight_baseline.return_value = b_day2
        api.get_prop_control.return_value = {"verified": True, "flat": True, "pending_entries": False, "ack": None}
        api.stop_buying.return_value = True
        api.start_trading.return_value = True
        guard = RiskGuard(api, self.state_file, profile=self.profile, autonomous_rollover=True, transient_grace_cycles=3)

        guard._check_drawdown(now=day2_grace)
        s1 = json.loads(self.state_file.read_text())
        self.assertFalse(s1["data_fault"])
        self.assertTrue(s1["transient_paused"])
        self.assertTrue(s1["wake_up_lock"])
        self.assertFalse(s1["daily_monitoring_available"])

        for i in range(3):
            tick = day2_ready + timedelta(seconds=5 * i)
            api.equity_observation = {
                "equity": 5000.0,
                "response_time_ms": int(tick.timestamp() * 1000),
                "source": "bybit_v5_wallet_response",
            }
            guard._check_drawdown(now=tick)
        s2 = json.loads(self.state_file.read_text())
        self.assertFalse(s2["data_fault"])
        self.assertFalse(s2.get("transient_paused", False))
        self.assertFalse(s2["macro_paused"])
        self.assertTrue(s2["daily_monitoring_available"])
        api.start_trading.assert_called()

    def test_a3_and_a4_midnight_reconcile_funding_parity_dca_partial_and_full_close(self):
        now = datetime(2026, 4, 12, 3, 30, 0, tzinfo=UTC)
        mid = now.replace(hour=0, minute=0, second=0, microsecond=0)
        mid_ms = int(mid.timestamp() * 1000)
        now_ms = int(now.timestamp() * 1000)

        # True midnight equity:
        # wallet_midnight = 4900.0
        # BTC midnight: Long 1.0 @ 100, mark@mid = 102 -> UPL = +2.0
        #   post-mid: Funding @ 00:00:00 (must skip!), +1.0 @ 90 (01:00), -0.5 @ 99 (02:00) -> now 1.5 @ avg 95
        # ETH midnight: Long 2.0 @ 50, mark@mid = 52 -> UPL = +4.0
        #   post-mid: full close -2.0 @ 55 (02:30, realized +10.0) -> now flat!
        # SOL midnight: flat (0)
        #   post-mid: open +4.0 @ 20 (03:00) -> now 4.0 @ 20
        # Tx changes after mid: -2.0 funding_fee +10.0 realized_pnl + 2.0 (ETH close vs mark) = +10.0 -> wallet_now = 4910.0
        api = Mock()
        api.id = "bybit"
        api.urls = {"api": {"private": "https://api-demo.bybit.com"}}
        api.implode_hostname = lambda url: url
        api.markets = {
            "BTC/USDT:USDT": {"symbol": "BTC/USDT:USDT", "id": "BTCUSDT", "swap": True, "linear": True, "settle": "USDT", "contractSize": 1},
            "ETH/USDT:USDT": {"symbol": "ETH/USDT:USDT", "id": "ETHUSDT", "swap": True, "linear": True, "settle": "USDT", "contractSize": 1},
            "SOL/USDT:USDT": {"symbol": "SOL/USDT:USDT", "id": "SOLUSDT", "swap": True, "linear": True, "settle": "USDT", "contractSize": 1},
        }
        api.privateGetV5UserQueryApi.return_value = {"retCode": 0, "time": now_ms, "result": {"userID": self.profile["account_id"], "readOnly": 0}}
        api.privateGetV5AccountInfo.return_value = {"retCode": 0, "time": now_ms, "result": {"unifiedMarginStatus": 3, "marginMode": "ISOLATED_MARGIN"}}
        # Attempt 1 sees wallet drift between pre-read (4909) and post-read (4910), Attempt 2 stays consistent at 4910.
        wallet_seq = ["4909.0", "4910.0", "4910.0", "4910.0"]

        def wallet_resp(_params):
            wb = wallet_seq.pop(0)
            eq = str(float(wb) + 7.5 + 4.0)
            return {
                "retCode": 0,
                "time": now_ms,
                "result": {"list": [{"accountType": "UNIFIED", "coin": [{"coin": "USDT", "equity": eq, "walletBalance": wb, "borrowAmount": "0", "unrealisedPnl": "11.5"}]}]},
            }

        api.privateGetV5AccountWalletBalance.side_effect = wallet_resp
        api.privateGetV5OrderRealtime.side_effect = lambda p: {"retCode": 0, "time": now_ms, "result": {"category": p["category"], "list": [], "nextPageCursor": ""}}
        api.privateGetV5PositionList.side_effect = lambda p: (
            {"retCode": 0, "time": now_ms, "result": {"category": p.get("category", "linear"), "list": [
                {"symbol": "BTCUSDT", "size": "1.5", "side": "Buy", "positionIdx": 0, "tradeMode": 1, "markPrice": "100.0", "avgPrice": "95.0", "unrealisedPnl": "7.5"},
                {"symbol": "SOLUSDT", "size": "4.0", "side": "Buy", "positionIdx": 0, "tradeMode": 1, "markPrice": "21.0", "avgPrice": "20.0", "unrealisedPnl": "4.0"},
            ], "nextPageCursor": ""}}
            if p.get("settleCoin") == "USDT"
            else {"retCode": 0, "time": now_ms, "result": {"category": p.get("category", "linear"), "list": [], "nextPageCursor": ""}}
        )
        api.privateGetV5AccountTransactionLog.return_value = {
            "retCode": 0, "time": now_ms,
            "result": {"list": [
                {"type": "REALIZED_PNL", "transactionTime": str(mid_ms + 9_000_000), "change": "10.0"},
            ], "nextPageCursor": ""},
        }
        api.privateGetV5ExecutionList.return_value = {
            "retCode": 0, "time": now_ms,
            "result": {"category": "linear", "list": [
                {"symbol": "BTCUSDT", "execType": "Funding", "side": "Buy", "execQty": "1.0", "execPrice": "102.0", "execTime": str(mid_ms)},
                {"symbol": "BTCUSDT", "execType": "Trade", "side": "Buy", "execQty": "1.0", "execPrice": "90.0", "closedSize": "0", "orderId": "btc-add", "execTime": str(mid_ms + 3_600_000)},
                {"symbol": "BTCUSDT", "execType": "Trade", "side": "Sell", "execQty": "0.5", "execPrice": "99.0", "closedSize": "0.5", "orderId": "btc-part", "execTime": str(mid_ms + 7_200_000)},
                {"symbol": "ETHUSDT", "execType": "Trade", "side": "Sell", "execQty": "2.0", "execPrice": "55.0", "closedSize": "2.0", "orderId": "eth-close-1", "execTime": str(mid_ms + 9_000_000)},
                {"symbol": "SOLUSDT", "execType": "Trade", "side": "Buy", "execQty": "4.0", "execPrice": "20.0", "closedSize": "0", "orderId": "sol-open", "execTime": str(mid_ms + 10_800_000)},
            ], "nextPageCursor": ""},
        }
        api.privateGetV5PositionClosedPnl.return_value = {
            "retCode": 0, "time": now_ms,
            "result": {"category": "linear", "list": [
                {"symbol": "ETHUSDT", "orderId": "eth-close-1", "avgEntryPrice": "50.0", "closedSize": "2.0"},
            ], "nextPageCursor": ""},
        }
        marks = {"BTCUSDT": "102.0", "ETHUSDT": "52.0"}
        api.publicGetV5MarketMarkPriceKline.side_effect = lambda p: {
            "retCode": 0, "time": now_ms,
            "result": {"list": [[str(mid_ms), marks[p["symbol"]], "0", "0", marks[p["symbol"]]]]},
        }

        sup = SupervisorBybit(api, self.profile)
        with patch("freqtrade.supervisor_bybit.datetime") as dt_mod, patch(
            "freqtrade.supervisor_bybit.time.time", return_value=now.timestamp()
        ), patch("freqtrade.snapshot_freshness.time.time", return_value=now.timestamp()):
            dt_mod.now.return_value = now
            reconciled = sup.reconcile_midnight_baseline(now)

        # Expected: wallet_mid = 4900.0 + upl_btc (+2.0) + upl_eth (+4.0) = 4906.0
        self.assertAlmostEqual(reconciled["equity"], 4906.0, places=5)
        self.assertEqual(reconciled["evidence"]["exec_count"], 4)  # Funding row skipped
        self.assertEqual([p["symbol"] for p in reconciled["evidence"]["positions_midnight"]], ["BTC/USDT:USDT", "ETH/USDT:USDT"])
        self.assertEqual(len(wallet_seq), 0)  # Verified parity loop ran attempt 1 (failed) + attempt 2 (passed)

    def test_a5_a8_a9_hyrotrader_rules_cap_multiday_derisk_and_inactivity(self):
        now = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)
        d1 = datetime(2026, 4, 15, 14, 0, 0, tzinfo=UTC)
        d2 = datetime(2026, 4, 16, 14, 0, 0, tzinfo=UTC)
        d3 = datetime(2026, 4, 17, 14, 0, 0, tzinfo=UTC)
        d4 = datetime(2026, 4, 18, 14, 0, 0, tzinfo=UTC)
        d5 = datetime(2026, 4, 19, 14, 0, 0, tzinfo=UTC)

        # A5 + A8:
        # Day1+Day2 in ONE still-open partial-exited trade (realized_profit=290.0:
        #   Day1 partial exit 600 notional -> +200 gross, Day2 partial exit 400 notional -> +100 gross, -10 fees),
        # plus Day3 (+75), Day4 (+75), Day5 (+160) closed trades ->
        # Day1 capped to $200, Days 2..5 add >= $300 -> counted >= $500 and 5 qualifying days -> PASSES even with Day1 > $200!
        open_derisk_trade = {
            "trade_id": 101,
            "is_open": True,
            "is_short": False,
            "realized_profit": 290.0,
            "open_timestamp": int(d1.timestamp() * 1000) - 60_000,
            "orders": [
                {"ft_is_entry": True, "is_open": False, "filled": 10.0, "average": 100.0, "cost": 1000.0, "order_filled_timestamp": int(d1.timestamp() * 1000) - 60_000},
                {"ft_is_entry": False, "is_open": False, "filled": 6.0, "average": 150.0, "cost": 900.0, "order_filled_timestamp": int(d1.timestamp() * 1000)},
                {"ft_is_entry": False, "is_open": False, "filled": 4.0, "average": 125.0, "cost": 500.0, "order_filled_timestamp": int(d2.timestamp() * 1000)},
            ],
        }
        closed_days = [
            {"trade_id": 102, "is_open": False, "close_timestamp": int(d3.timestamp() * 1000), "profit_abs": 75.0, "stake_amount": 300.0, "profit_ratio": 0.25},
            {"trade_id": 103, "is_open": False, "close_timestamp": int(d4.timestamp() * 1000), "profit_abs": 75.0, "stake_amount": 300.0, "profit_ratio": 0.25},
            {"trade_id": 104, "is_open": False, "close_timestamp": int(d5.timestamp() * 1000), "profit_abs": 160.0, "stake_amount": 400.0, "profit_ratio": 0.40},
        ]

        rm = RiskManager.__new__(RiskManager)
        rm.profile = {**self.profile, "phase_start_utc": "2026-04-01T00:00:00Z"}
        rm.initial_balance = 5000.0
        rm.state = {"nuclear_triggered": False}
        rm._shutdown = SimpleNamespace(is_set=Mock(side_effect=[False, True]), wait=Mock())
        rm.request_rules_pause = Mock()
        rm.rules_api = SimpleNamespace(get_open_trades=lambda: [open_derisk_trade], get_trades=lambda limit=500: closed_days)
        rm._hyrotrader_rules_loop()
        rm.request_rules_pause.assert_called_once_with(True, "HyroTrader One-Step profit target and qualifying days achieved")

        # A9: Open trade sitting untouched for 26 days triggers critical inactivity alert without forceenter.
        stale_now = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
        stale_entry = stale_now - timedelta(days=26)
        stagnant_trade = {
            "trade_id": 201,
            "is_open": True,
            "realized_profit": 0.0,
            "open_timestamp": int(stale_entry.timestamp() * 1000),
            "orders": [
                {"ft_is_entry": True, "is_open": False, "filled": 1.0, "average": 100.0, "cost": 100.0, "order_filled_timestamp": int(stale_entry.timestamp() * 1000)},
            ],
        }
        rm2 = RiskManager.__new__(RiskManager)
        rm2.profile = {**self.profile, "phase_start_utc": "2026-04-01T00:00:00Z"}
        rm2.initial_balance = 5000.0
        rm2.state = {"nuclear_triggered": False}
        rm2._shutdown = SimpleNamespace(is_set=Mock(side_effect=[False, True]), wait=Mock())
        rm2.request_rules_pause = Mock()
        rm2.inactivity_keepalive_enabled = True
        rm2.rules_api = SimpleNamespace(
            get_open_trades=lambda: [stagnant_trade],
            get_trades=lambda limit=500: [],
            post=Mock(side_effect=AssertionError("Keepalive must not open second trade while one is open")),
        )
        with patch("llm_risk_manager_prop.logger") as log_mock, patch("llm_risk_manager_prop.datetime") as dt_mock:
            dt_mock.now.return_value = stale_now
            dt_mock.min = datetime.min
            dt_mock.fromisoformat = datetime.fromisoformat
            dt_mock.fromtimestamp = datetime.fromtimestamp
            rm2._hyrotrader_rules_loop()
        self.assertTrue(any("24-DAY INACTIVITY ALERT" in str(c) for c in log_mock.critical.call_args_list))

    def test_midnight_transient_rollover_atomicity_and_pre_baseline_outage_no_fault(self):
        day1 = datetime(2026, 11, 10, 23, 59, 55, tzinfo=UTC)
        day2_outage = datetime(2026, 11, 11, 0, 0, 2, tzinfo=UTC)
        day2_recover = datetime(2026, 11, 11, 0, 0, 7, tzinfo=UTC)

        b_day1 = baseline(self.profile, now=day1)
        state_day1 = reconciled_state(self.profile, b_day1, day1, halted=False)
        state_day1["data_valid"] = True
        state_day1["last_updated"] = day1.isoformat()

        journal = OrderJournal(
            self.root,
            {
                "account_id": self.profile["account_id"],
                "challenge_id": self.profile["challenge_id"],
                "phase_id": self.profile["phase_id"],
                "profile_id": self.profile["profile_id"],
                "exchange": self.profile["exchange"],
                "environment": self.profile["environment"],
                "currency": self.profile["currency"],
                "key_fingerprint": hashlib.sha256(b"offline").hexdigest(),
            },
        )
        self.addCleanup(journal.close)
        bot = SimpleNamespace(
            config={**self.account_cfg, "prop_guard_enabled": True, "user_data_dir": str(self.root)},
            order_journal=journal,
        )

        # BUG-1 regression: transient rollover error never writes an intermediate unlocked baseline=None state.
        (self.root / "risk_manager_prop_baseline.json").write_text(json.dumps(b_day1))
        self.state_file.write_text(json.dumps(state_day1))
        api = Mock()
        api.get_prop_profile.return_value = self.profile
        api.get_equity.return_value = 5000.0
        api.equity_observation = {
            "equity": 5000.0,
            "response_time_ms": int(day2_outage.timestamp() * 1000),
            "source": "bybit_v5_wallet_response",
        }
        api.reconcile_midnight_baseline.side_effect = UnverifiedAccount("Temporary Bybit timeout")
        api.get_prop_control.return_value = {"verified": True, "flat": True, "pending_entries": False, "ack": None}
        api.stop_buying.return_value = True
        api.start_trading.return_value = True

        guard = RiskGuard(api, self.state_file, profile=self.profile, autonomous_rollover=True, transient_grace_cycles=3)
        saved_snapshots = []
        orig_save = guard._save_state

        def _spy_save(*, force_emergency=False):
            ok = orig_save(force_emergency=force_emergency)
            saved_snapshots.append(json.loads(self.state_file.read_text()))
            return ok

        guard._save_state = _spy_save
        guard._check_drawdown(now=day2_outage)
        self.assertEqual(len(saved_snapshots), 1)
        self.assertTrue(saved_snapshots[0]["transient_paused"])
        self.assertTrue(saved_snapshots[0]["wake_up_lock"])
        self.assertFalse(saved_snapshots[0]["data_fault"])
        self.assertFalse(saved_snapshots[0]["data_valid"])
        with patch("freqtrade.prop_controls.datetime") as dt_mock:
            dt_mock.now.return_value = day2_outage
            dt_mock.fromisoformat = datetime.fromisoformat
            self.assertTrue(sync_prop_controls(bot))
        self.assertFalse(read_control(journal)["data_fault"])
        self.assertIsNone(RiskGuard(api, self.state_file, profile=self.profile, autonomous_rollover=True).state_error)

        # BUG-2 regression: transient outage before baseline rollover clears stale yesterday baseline cleanly.
        self.state_file.write_text(json.dumps(state_day1))
        api2 = Mock()
        api2.get_prop_profile.return_value = self.profile
        api2.get_equity.return_value = None  # Triggers _pause_on_transient BEFORE _check_drawdown baseline rollover.
        api2.stop_buying.return_value = True
        api2.start_trading.return_value = True
        api2.get_prop_control.return_value = {"verified": True, "flat": True, "pending_entries": False, "ack": None}
        guard2 = RiskGuard(api2, self.state_file, profile=self.profile, autonomous_rollover=True, transient_grace_cycles=3)
        guard2._check_drawdown(now=day2_outage)

        s_outage = json.loads(self.state_file.read_text())
        self.assertIsNone(s_outage["baseline"])
        self.assertFalse(s_outage["data_valid"])
        self.assertFalse(s_outage["data_fault"])
        self.assertTrue(s_outage["transient_paused"])
        self.assertTrue(s_outage["wake_up_lock"])
        self.assertEqual(s_outage["current_trading_day"], "2026-11-11")

        # Restarting supervisor + syncing core on that outage snapshot must NOT trigger state_error or sticky data_fault.
        guard2_restarted = RiskGuard(
            api2, self.state_file, profile=self.profile, autonomous_rollover=True, transient_grace_cycles=3
        )
        self.assertIsNone(guard2_restarted.state_error)
        with patch("freqtrade.prop_controls.datetime") as dt_mock:
            dt_mock.now.return_value = day2_outage
            dt_mock.fromisoformat = datetime.fromisoformat
            self.assertTrue(sync_prop_controls(bot))
        self.assertFalse(read_control(journal)["data_fault"])

        # Recovery: equity + reconciled Day 2 baseline arrive -> self-resumes after 3 healthy cycles.
        b_day2 = baseline(self.profile, now=day2_recover)
        api2.get_equity.return_value = 5000.0
        api2.reconcile_midnight_baseline.return_value = b_day2
        for i in range(3):
            tick = day2_recover + timedelta(seconds=5 * i)
            api2.equity_observation = {
                "equity": 5000.0,
                "response_time_ms": int(tick.timestamp() * 1000),
                "source": "bybit_v5_wallet_response",
            }
            guard2_restarted._check_drawdown(now=tick)
        s_recovered = json.loads(self.state_file.read_text())
        self.assertTrue(s_recovered["data_valid"])
        self.assertFalse(s_recovered["data_fault"])
        self.assertFalse(s_recovered["transient_paused"])
        self.assertFalse(s_recovered["wake_up_lock"])
        api2.start_trading.assert_called()

    def test_prop_mode_process_runs_pre_analyze_sync_without_duplicate_replace(self):
        calls = []
        fake_bot = SimpleNamespace(
            config={"prop_guard_enabled": True},
            _exit_lock=nullcontext(),
            _measure_execution=nullcontext(),
            manage_open_orders=lambda allow_replace=True: calls.append(allow_replace),
            exchange=SimpleNamespace(reload_markets=lambda: None),
            update_trades_without_assigned_fees=lambda: None,
            _refresh_active_whitelist=lambda t: [],
            pairlists=SimpleNamespace(create_pair_list=lambda w: []),
            strategy=SimpleNamespace(
                gather_informative_pairs=lambda: [],
                bot_loop_start=lambda **kw: None,
                analyze=lambda w: None,
                position_adjustment_enable=False,
            ),
            dataprovider=SimpleNamespace(refresh=lambda *a: None, _msg_queue=[]),
            exit_positions=lambda t: None,
            state=None,
            _schedule=SimpleNamespace(run_pending=lambda: None),
            rpc=SimpleNamespace(process_msg_queue=lambda q: None),
            last_process=None,
        )
        with patch("freqtrade.freqtradebot.require_core_authority"), patch(
            "freqtrade.freqtradebot.sync_prop_controls"
        ), patch("freqtrade.freqtradebot.cancel_fault_entries"), patch(
            "freqtrade.freqtradebot.reconcile_journal"
        ), patch("freqtrade.freqtradebot.Trade.get_open_trades", return_value=[]), patch(
            "freqtrade.freqtradebot.Trade.commit"
        ):
            FreqtradeBot.process(fake_bot)
        self.assertEqual(calls, [False, True])

    def test_bybit_descending_mark_kline_and_ccxt_rejected_empty_quantities(self):
        import ccxt

        from freqtrade.order_recovery import order_evidence_valid, verified_prop_cancel

        # BUG-NEW-1: Bybit V5 returns mark-price-kline newest-first;
        # without "end", limit=1 misses midnight.
        day2_mid = datetime(2026, 11, 12, 0, 0, 0, tzinfo=UTC)
        mid_ms = int(day2_mid.timestamp() * 1000)
        later_now = day2_mid + timedelta(minutes=5)
        helper = AutonomousRolloverAndTransientTests()
        helper.profile = self.profile
        direct = helper._bybit_adapter(
            day2_mid, equity=5060.0, wallet=5000.0, mark_mid=102.0, clock=[later_now]
        )
        later_ms = int(later_now.timestamp() * 1000)
        all_bars = [
            [str(mid_ms + 120_000), "115.0", "115.0", "115.0", "115.0"],
            [str(mid_ms + 60_000), "108.0", "108.0", "108.0", "108.0"],
            [str(mid_ms), "102.0", "102.0", "102.0", "102.0"],
        ]

        def descending_kline(params):
            start = int(params["start"])
            end = int(params.get("end", later_ms))
            limit = int(params.get("limit", 200))
            sliced = [b for b in all_bars if start <= int(b[0]) <= end][:limit]
            return {
                "retCode": 0,
                "time": later_ms,
                "result": {"category": "linear", "symbol": "SOLUSDT", "list": sliced},
            }

        direct.api.publicGetV5MarketMarkPriceKline.side_effect = descending_kline
        with patch("freqtrade.supervisor_bybit.time.time", return_value=later_now.timestamp()):
            reconciled = direct.reconcile_midnight_baseline(later_now)
        self.assertAlmostEqual(reconciled["equity"], 5006.0, places=6)

        # BUG-NEW-2: real ccxt.bybit().parse_order() yields filled=None, remaining=None on Rejected.
        ex = ccxt.bybit({"options": {"defaultType": "swap"}})
        market = {
            "id": "SOLUSDT", "symbol": "SOL/USDT:USDT", "type": "swap", "swap": True,
            "spot": False, "linear": True, "inverse": False, "contract": True,
            "option": False, "future": False, "settle": "USDT", "base": "SOL",
            "quote": "USDT", "contractSize": 1.0,
            "precision": {"amount": 0.1, "price": 0.01},
            "limits": {"amount": {"min": 0.1, "max": 1000}}, "info": {},
        }
        ex.set_markets([market])
        direct.api.markets["SOLUSDT"].update(market)
        cid = "c" * 32
        raw_rej = {
            "orderId": "ord-rej-1", "orderLinkId": cid, "symbol": "SOLUSDT", "side": "Buy",
            "orderType": "Limit", "price": "100", "qty": "3", "cumExecQty": "",
            "leavesQty": "", "avgPrice": "", "orderStatus": "Rejected",
            "reduceOnly": False, "positionIdx": 0,
        }
        direct.api.parse_order.side_effect = ex.parse_order
        direct.api.privateGetV5OrderRealtime.side_effect = lambda *_a, **_kw: {
            "retCode": 0,
            "time": later_ms,
            "result": {"category": "linear", "list": [], "nextPageCursor": ""},
        }
        direct.api.privateGetV5OrderHistory.side_effect = lambda *_a, **_kw: {
            "retCode": 0,
            "time": later_ms,
            "result": {"category": "linear", "list": [raw_rej], "nextPageCursor": ""},
        }
        direct.api.privateGetV5ExecutionList.side_effect = lambda *_a, **_kw: {
            "retCode": 0,
            "time": later_ms,
            "result": {"category": "linear", "list": [], "nextPageCursor": ""},
        }
        with patch("freqtrade.supervisor_bybit.time.time", return_value=later_now.timestamp()):
            looked_up = direct.lookup("SOL/USDT:USDT", client_id=cid)
        self.assertEqual(looked_up["status"], "rejected")
        self.assertEqual(looked_up["filled"], 0.0)
        self.assertEqual(looked_up["remaining"], 0.0)

        parsed_rej = ex.parse_order(raw_rej, market)
        self.assertIsNone(parsed_rej["filled"])
        local_order = SimpleNamespace(order_id="ord-rej-1", side="buy", amount=3.0, filled=0.0)
        self.assertTrue(order_evidence_valid(parsed_rej, local_order, "SOL/USDT:USDT"))
        self.assertFalse(
            order_evidence_valid({**parsed_rej, "status": "open"}, local_order, "SOL/USDT:USDT")
        )
        self.assertFalse(
            order_evidence_valid(
                {**parsed_rej, "status": "canceled", "filled": 1.0, "remaining": None},
                local_order,
                "SOL/USDT:USDT",
            )
        )
        fake_trade = SimpleNamespace(
            pair="SOL/USDT:USDT",
            get_custom_data=lambda **_kw: [],
            set_custom_data=lambda **_kw: None,
        )
        fake_bot = SimpleNamespace(
            config={"prop_guard_enabled": True},
            order_journal=None,
            exchange=SimpleNamespace(fetch_order=lambda *_a: dict(parsed_rej)),
        )
        with patch("freqtrade.emergency_authority.require_core_authority"):
            canceled = verified_prop_cancel(
                fake_bot, fake_trade, local_order, SimpleNamespace(commit=lambda: None)
            )
        self.assertIsNotNone(canceled)
        self.assertEqual(canceled["filled"], 0.0)
        self.assertEqual(canceled["remaining"], 0.0)

        journal = OrderJournal(
            self.root / "j_rej",
            {
                "account_id": self.profile["account_id"],
                "challenge_id": self.profile["challenge_id"],
                "phase_id": self.profile["phase_id"],
                "profile_id": self.profile["profile_id"],
                "exchange": self.profile["exchange"],
                "environment": self.profile["environment"],
                "currency": self.profile["currency"],
                "key_fingerprint": hashlib.sha256(b"rej").hexdigest(),
            },
        )
        self.addCleanup(journal.close)
        from snapshot_fixtures import timed_snapshot

        init_snap = timed_snapshot(
            {"complete": True, "open_orders": [], "positions": [], "orders": {}, "fills": {}}
        )
        self.assertEqual(journal.reconcile(init_snap, {}, {}), [])
        j_cid = journal.prepare(
            {"pair": "SOL/USDT:USDT", "side": "buy", "amount": 3.0, "reduceOnly": False}
        )
        journal.dispatch(
            j_cid,
            lambda **_kw: {"id": "ord-rej-1", "clientOrderId": j_cid},
        )
        snap = timed_snapshot(
            {
                "complete": True,
                "open_orders": [],
                "positions": [],
                "orders": {j_cid: {**parsed_rej, "clientOrderId": j_cid}},
                "fills": {j_cid: 0.0},
            }
        )
        self.assertEqual(journal.reconcile(snap, {}, {}), [])
        self.assertEqual(journal.intents(False)[0]["state"], "rejected")
        self.assertEqual(journal.intents(False)[0]["settled"], 1)

    def test_save_state_failure_single_fault_and_midnight_window_post_budget_pause(self):
        # BUG-NEW-3: _save_state() failure with baseline_error calls _pause_on_fault once.
        day1 = datetime(2026, 11, 10, 23, 59, 45, tzinfo=UTC)
        day2 = datetime(2026, 11, 11, 0, 0, 2, tzinfo=UTC)
        b_day1 = baseline(self.profile, now=day1)
        state_day1 = reconciled_state(self.profile, b_day1, day1, halted=False)
        state_day1["data_valid"] = True
        self.state_file.write_text(json.dumps(state_day1))

        api = Mock()
        api.get_prop_profile.return_value = self.profile
        api.get_equity.return_value = 5000.0
        api.stop_buying.return_value = True
        api.reconcile_midnight_baseline.side_effect = ValueError("No non-transient fallback")
        guard = RiskGuard(
            api,
            self.state_file,
            profile=self.profile,
            autonomous_rollover=True,
            transient_grace_cycles=3,
        )

        def fail_save(*, force_emergency=False):
            guard.state_error = "State write failed: disk full"
            return False

        guard._save_state = fail_save
        fault_calls = []
        guard._pause_on_fault = lambda r: fault_calls.append(r)
        guard._check_drawdown(now=day2)
        self.assertEqual(fault_calls, ["State write failed: disk full"])

        # BUG-NEW-4: 23:59:57 UTC receives 12s rollover budget; deadline pause runs outside budget.
        self.state_file.write_text(json.dumps(state_day1))
        in_budget = [False]

        class BudgetTracker:
            def __init__(self):
                self.budgets = []
                self.stop_calls_inside_budget = []

            from contextlib import contextmanager as _cm

            @_cm
            def operation_budget(self, seconds=4.0):
                self.budgets.append(seconds)
                in_budget[0] = True
                try:
                    yield
                finally:
                    in_budget[0] = False

            def deadline_expired(self):
                return True

            def get_prop_profile(self):
                return self_profile

            def get_equity(self):
                return 5000.0

            def get_prop_control(self):
                return {"verified": True, "flat": True, "pending_entries": False, "ack": None}

            def stop_buying(self):
                self.stop_calls_inside_budget.append(in_budget[0])
                guard_run._shutdown.set()
                return True

        self_profile = self.profile
        tracker = BudgetTracker()
        guard_run = RiskGuard(
            tracker,
            self.state_file,
            profile=self.profile,
            autonomous_rollover=True,
            transient_grace_cycles=3,
        )
        self.assertFalse(
            guard_run._needs_midnight_rollover(now=datetime(2026, 11, 10, 23, 59, 45, tzinfo=UTC))
        )
        self.assertTrue(
            guard_run._needs_midnight_rollover(now=datetime(2026, 11, 10, 23, 59, 57, tzinfo=UTC))
        )

        def fail_drawdown():
            raise TimeoutError("Bybit slow at midnight")

        guard_run._check_drawdown = fail_drawdown
        guard_run._on_clean_shutdown = lambda: None
        guard_run.run()
        self.assertEqual(tracker.stop_calls_inside_budget, [False])
        self.assertEqual(tracker.budgets, [12.0])
        self.assertTrue(guard_run.state["transient_paused"])


if __name__ == "__main__":
    unittest.main()
