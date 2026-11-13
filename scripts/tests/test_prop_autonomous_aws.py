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


if __name__ == "__main__":
    unittest.main()
