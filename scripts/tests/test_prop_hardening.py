"""Offline regression tests: no API credentials, exchange sessions, or live DB."""

import importlib.util
import json
import math
import random
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[2]
(ROOT / "user_data/forensic").mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "scripts"))
from llm_risk_manager_prop import BinanceMacroMonitor, FreqtradeAPIClient  # noqa: E402
from prop_risk_guard import RiskGuard, market_crash  # noqa: E402


spec = importlib.util.spec_from_file_location(
    "prop_strategy", ROOT / "user_data/strategies/NostalgiaForInfinityX8_Prop.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
Strategy = module.NostalgiaForInfinityX8_Prop
NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / "user_data/forensic")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.api = MagicMock()
        self.api.get_equity.return_value = 5000.0
        self.api.get_open_trades.return_value = []
        self.api.stop_buying.return_value = True
        self.api.force_exit.return_value = True
        self.guard = RiskGuard(self.api, self.path)

    def test_daily_boundary_and_restart_latch(self):
        self.assertFalse(self.guard._check_drawdown(NOW))
        self.api.get_equity.return_value = 4900
        self.assertTrue(self.guard._check_drawdown(NOW + timedelta(seconds=5)))
        restored = RiskGuard(self.api, self.path)
        self.assertTrue(restored.nuclear_triggered)
        restored._check_drawdown(NOW + timedelta(seconds=10))
        self.assertTrue(restored.state["liquidation_confirmed"])
        self.api.start_trading.assert_not_called()

    def test_macro_halt_before_first_equity_sample_survives_restart(self):
        self.guard.trigger_nuclear_option("crash during startup")
        restored = RiskGuard(self.api, self.path)
        self.assertIsNone(restored.state_error)
        self.assertTrue(restored.nuclear_triggered)

    def test_trailing_limit_survives_new_day(self):
        self.guard._check_drawdown(NOW)
        self.api.get_equity.return_value = 5300
        self.guard._check_drawdown(NOW + timedelta(minutes=1))
        self.api.get_equity.return_value = 5125
        self.assertTrue(self.guard._check_drawdown(NOW + timedelta(days=1)))
        self.assertIn("trailing", self.guard.state["halt_reason"])

    def test_utc_day_rollover_cannot_reset_at_loss(self):
        self.guard._check_drawdown(NOW)
        self.api.get_equity.return_value = 4900
        self.assertTrue(self.guard._check_drawdown(NOW + timedelta(days=1)))
        self.assertEqual(self.guard.state["day_start_balance"], 5000)

    def test_corrupt_state_preserved_and_paused(self):
        self.path.write_text("{broken")
        guard = RiskGuard(self.api, self.path)
        guard._check_drawdown(NOW)
        self.assertEqual(self.path.read_text(), "{broken")
        self.api.stop_buying.assert_called()
        self.api.get_equity.assert_not_called()

    def test_invalid_equity_locks_entries(self):
        for bad in (None, math.nan, math.inf, -math.inf, True, "5000"):
            self.api.get_equity.return_value = bad
            self.guard._check_drawdown(NOW)
            self.assertTrue(self.guard.state["data_fault"])
            self.assertTrue(self.guard.state["wake_up_lock"])

    def test_zero_and_negative_equity_liquidate(self):
        for equity in (0, -1):
            guard = RiskGuard(self.api, self.path.with_name(f"{equity}.json"))
            self.api.get_equity.return_value = equity
            self.assertTrue(guard._check_drawdown(NOW))

    def test_failed_or_unknown_liquidation_retried(self):
        self.api.get_open_trades.return_value = [{"trade_id": 3}]
        self.api.force_exit.return_value = False
        self.guard.trigger_nuclear_option("test")
        self.assertFalse(self.guard.state["liquidation_confirmed"])
        self.api.get_open_trades.return_value = None
        self.guard._check_drawdown(NOW)
        self.assertFalse(self.guard.state["liquidation_confirmed"])
        self.api.get_open_trades.return_value = []
        self.guard._check_drawdown(NOW)
        self.assertTrue(self.guard.state["liquidation_confirmed"])
        self.assertEqual(self.api.stop_buying.call_count, 3)

    def test_persistence_failure_pauses(self):
        with patch("prop_risk_guard.os.replace", side_effect=OSError("disk full")):
            self.guard._check_drawdown(NOW)
        self.assertIsNotNone(self.guard.state_error)
        self.api.stop_buying.assert_called()

    def test_restart_does_not_rebase_profit_or_loss(self):
        self.api.get_equity.return_value = 4990
        self.assertTrue(self.guard._check_drawdown(NOW))
        self.assertEqual(self.guard.initial_balance, 5000)

    def test_unsafe_configuration_rejected(self):
        for kwargs in (
            {"initial": 0},
            {"daily_limit": 0.05},
            {"trailing_limit": 0.08},
            {"interval": 30},
        ):
            with self.assertRaises(ValueError):
                RiskGuard(self.api, self.path, **kwargs)

    def test_no_trade_history_or_llm_on_critical_path(self):
        self.guard._check_drawdown(NOW)
        self.api.get_trades.assert_not_called()
        self.api.get_open_trades.assert_not_called()


class AccountingAndMacroTests(unittest.TestCase):
    def test_full_account_total_only_no_double_pnl(self):
        client = FreqtradeAPIClient("http://invalid", "", "")
        snapshot = {
            "value": 4960,
            "currency": "USDT",
            "source": "bybit_wallet_coin_equity",
            "timestamp": int(time.time() * 1000),
        }
        client.get_balance = MagicMock(
            return_value={"total": 4600, "total_bot": 1160, "account_equity": snapshot}
        )
        self.assertEqual(client.get_equity(), 4960)
        for value in (0, -5):
            client.get_balance.return_value = {"account_equity": snapshot | {"value": value}}
            self.assertEqual(client.get_equity(), value)
        for bad in (None, math.inf, math.nan, True, "5000"):
            client.get_balance.return_value = {
                "account_equity": snapshot | {"value": bad},
                "total": 5000,
            }
            self.assertIsNone(client.get_equity())
        client.get_balance.return_value = {"total": 5000, "total_bot": 5000}
        self.assertIsNone(client.get_equity())
        client.get_balance.return_value = {"account_equity": snapshot | {"timestamp": 1}}
        self.assertIsNone(client.get_equity())

    def test_bybit_equity_ignores_pending_margin_and_counts_upnl_once(self):
        from freqtrade.enums import TradingMode
        from freqtrade.exchange.bybit import Bybit

        exchange = SimpleNamespace(
            trading_mode=TradingMode.FUTURES, _config={"stake_currency": "USDT"}
        )
        coin = {
            "coin": "USDT",
            "walletBalance": "5000",
            "equity": "4960",
            "unrealisedPnl": "-40",
            "totalOrderIM": "400",
            "totalPositionIM": "400",
        }
        payload = {
            "retCode": 0,
            "time": 1793660000000,
            "result": {"list": [{"accountType": "UNIFIED", "coin": [coin]}]},
        }
        snapshot = Bybit._parse_account_equity(exchange, payload)
        self.assertEqual(snapshot["value"], 4960)
        payload["result"]["list"][0]["coin"].append(
            {"coin": "BTC", "equity": "1", "walletBalance": "1"}
        )
        self.assertIsNone(Bybit._parse_account_equity(exchange, payload))
        for bad in (None, {}, {"retCode": 0, "result": {"list": []}}):
            self.assertIsNone(Bybit._parse_account_equity(exchange, bad))

    def test_price_only_crash_confirmation(self):
        self.assertTrue(market_crash({"btc_1h_pct": -4.0}))
        self.assertTrue(market_crash({"btc_24h_pct": -8.0}))
        self.assertFalse(market_crash({"is_flash_crash": True, "btc_1h_pct": math.nan}))
        self.assertFalse(market_crash({"btc_1h_pct": 4, "btc_24h_pct": 8}))

    def test_http_failure_cannot_return_calm_or_stale_metrics(self):
        monitor = BinanceMacroMonitor()
        monitor._cached_metrics = {"btc_1h_pct": 0, "btc_24h_pct": 0}
        with patch("llm_risk_manager_prop.requests.get", side_effect=TimeoutError()):
            metrics = monitor.get_metrics()
        self.assertIsNone(metrics["btc_1h_pct"])


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.s = Strategy.__new__(Strategy)
        self.s.wallets = MagicMock()
        self.s.wallets.get_total_stake_amount.return_value = 1200.0
        self.s.config = {
            "exchange": {"name": "bybit"},
            "runmode": SimpleNamespace(value="backtest"),
        }
        self.s.is_futures_mode = True

    def test_leverage_ignores_every_override(self):
        for value in (0, -1, 100, None, math.nan, "bad"):
            self.s.futures_mode_leverage = value
            self.s.futures_mode_leverage_rebuy_mode = value
            for tag in (None, "", "61", "120", "1"):
                self.assertEqual(self.s.leverage("BTC", NOW, 1, value, value, tag, "long"), 1.0)

    def test_10000_random_sizing_cases(self):
        rng = random.Random(20261102)  # noqa: S311 - reproducible adversarial inputs, not secrets
        for _ in range(10000):
            proposed, minimum, maximum = [rng.uniform(0, 1000) for _ in range(3)]
            stake = self.s.custom_stake_amount(
                "BTC", NOW, 1, proposed, minimum, maximum, 1, None, "long"
            )
            self.assertLessEqual(stake, min(400, proposed, maximum))
            self.assertTrue(stake == 0 or stake >= minimum)

    def test_minimum_never_forces_oversize(self):
        self.assertEqual(
            self.s.custom_stake_amount("BTC", NOW, 1, 400, 401, 5000, 1, None, "long"), 0
        )
        self.assertEqual(self.s.correct_min_stake(None, 0), 5)
        self.assertEqual(self.s.adjust_trade_position(None, NOW, 1, -0.2, 1, 5000), None)

    def test_safety_exits_bypass_hold_and_profit_filters(self):
        for reason in (
            "stop_loss",
            "trailing_stop_loss",
            "emergency_exit",
            "force_exit",
            "liquidation",
        ):
            self.assertTrue(
                self.s.confirm_trade_exit("BTC", None, "market", 1, 1, "GTC", reason, NOW)
            )

    def test_actual_order_value_checked_even_for_force_entry(self):
        self.assertFalse(
            self.s.confirm_trade_entry("BTC", "limit", 401, 1, "GTC", NOW, "force_entry", "long")
        )
        self.assertFalse(
            self.s.confirm_trade_entry("BTC", "limit", 1, 1, "GTC", NOW, "force_entry", "short")
        )

    def test_missing_stale_or_latched_heartbeat_blocks_entry(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "user_data/forensic") as temp:
            self.s.config.update(user_data_dir=temp, runmode=SimpleNamespace(value="dry_run"))

            def allowed():
                return self.s.confirm_trade_entry(
                    "BTC", "limit", 100, 1, "GTC", NOW, "force_entry", "long"
                )

            self.assertFalse(allowed())
            p = Path(temp) / "risk_manager_prop_state.json"
            base = {
                "schema_version": 1,
                "last_updated": NOW.isoformat(),
                "nuclear_triggered": False,
                "data_fault": False,
                "wake_up_lock": False,
            }
            p.write_text(json.dumps(base))
            self.assertTrue(allowed())
            for changes in (
                {"nuclear_triggered": True},
                {"data_fault": True},
                {"last_updated": (NOW - timedelta(seconds=16)).isoformat()},
            ):
                p.write_text(json.dumps(base | changes))
                self.assertFalse(allowed())

    def test_informative_candles_visible_only_when_closed(self):
        import pandas as pd

        from freqtrade.strategy import merge_informative_pair

        for tf, minutes in [("15m", 15), ("1h", 60), ("4h", 240), ("1d", 1440)]:
            dates = pd.date_range("2024-01-01", periods=minutes // 5 + 2, freq="5min", tz="UTC")
            base = pd.DataFrame({"date": dates})
            info = pd.DataFrame({"date": [dates[0]], "close": [999.0]})
            merged = merge_informative_pair(base, info, "5m", tf)
            first = merged[f"close_{tf}"].first_valid_index()
            self.assertEqual(first, minutes // 5 - 1)


class EquityReconstructionTests(unittest.TestCase):
    def test_floating_loss_is_visible_even_when_trade_closes_flat(self):
        import pandas as pd
        from forensic_prop_validation import equity_metrics

        with tempfile.TemporaryDirectory(dir=ROOT / "user_data/forensic") as temp:
            data = Path(temp) / "futures"
            data.mkdir()
            dates = pd.date_range("2024-01-01", periods=3, freq="5min", tz="UTC")
            pd.DataFrame({"date": dates, "close": [100, 80, 100]}).to_feather(
                data / "BTC_USDT_USDT-5m-futures.feather"
            )
            stamps = [int(d.timestamp() * 1000) for d in dates]
            summary = {
                "starting_balance": 5000,
                "backtest_start_ts": stamps[0],
                "backtest_end_ts": stamps[-1],
                "trades": [
                    {
                        "pair": "BTC/USDT:USDT",
                        "amount": 4,
                        "leverage": 1,
                        "is_short": False,
                        "fee_open": 0,
                        "fee_close": 0,
                        "close_timestamp": stamps[-1],
                        "profit_abs": 0,
                        "orders": [
                            {
                                "amount": 4,
                                "safe_price": 100,
                                "ft_is_entry": True,
                                "order_filled_timestamp": stamps[0],
                            },
                            {
                                "amount": 4,
                                "safe_price": 100,
                                "ft_is_entry": False,
                                "order_filled_timestamp": stamps[-1],
                            },
                        ],
                    }
                ],
            }
            result = equity_metrics(summary, temp)
            self.assertAlmostEqual(result["max_daily_dd_pct"], 1.6)
            self.assertAlmostEqual(result["max_trailing_dd_pct"], 1.6)
            self.assertAlmostEqual(result["net_profit_pct"], 0)


if __name__ == "__main__":
    unittest.main()
