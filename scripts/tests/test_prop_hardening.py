"""Offline regression tests: no API credentials, exchange sessions, or live DB."""

import importlib.util
import json
import math
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


# Versioned static-floor guard regressions live in test_prop_profile.py.


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

    def test_native_dca_and_sizing_are_restored(self):
        import ast

        def methods(path):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
            return {
                n.name: ast.dump(n, include_attributes=False)
                for n in cls.body
                if isinstance(n, ast.FunctionDef)
            }

        directory = ROOT / "user_data/strategies"
        native = methods(directory / "NostalgiaForInfinityX8.py")
        prop = methods(directory / "NostalgiaForInfinityX8_Prop.py")
        for name in (
            "custom_stake_amount",
            "adjust_trade_position",
            "custom_exit",
            "populate_indicators",
            "populate_entry_trend",
            "populate_exit_trend",
        ):
            self.assertEqual(native[name], prop[name], name)
        self.assertTrue(self.s.position_adjustment_enable)
        self.assertTrue(self.s.grinding_enable)
        self.assertEqual(self.s.stoploss, -0.99)
        self.assertEqual(self.s.max_entry_position_adjustment, -1)

    def test_leverage_ignores_every_override(self):
        for value in (0, -1, 100, None, math.nan, "bad"):
            self.s.futures_mode_leverage = value
            self.s.futures_mode_leverage_rebuy_mode = value
            for tag in (None, "", "61", "120", "1"):
                self.assertEqual(self.s.leverage("BTC", NOW, 1, value, value, tag, "long"), 1.0)

    def test_runtime_profile_requires_wallet_gate_and_native_dca(self):
        self.s.config.update(
            trading_mode="futures",
            margin_mode="isolated",
            max_open_trades=3,
            tradable_balance_ratio=0.24,
            prop_guard_enabled=True,
            runmode=SimpleNamespace(value="dry_run"),
        )
        self.s.order_types = {
            "stoploss": "market",
            "force_exit": "market",
            "stoploss_on_exchange": False,
        }
        self.s.bot_start()
        for key, value in (
            ("available_capital", 5000),
            ("tradable_balance_ratio", 0.50),
            ("prop_guard_enabled", False),
        ):
            original = self.s.config.copy()
            self.s.config[key] = value
            with self.assertRaises(ValueError):
                self.s.bot_start()
            self.s.config = original
        self.s.position_adjustment_enable = False
        with self.assertRaises(ValueError):
            self.s.bot_start()

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

    def test_short_orders_rejected_even_for_force_entry(self):
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


class WalletCapitalGateTests(unittest.TestCase):
    def test_stale_supervisor_blocks_dca_capital_without_changing_strategy(self):
        from freqtrade.wallets import Wallets
        from test_prop_profile import configured_account, baseline
        from prop_risk_guard import reconciled_state
        from freqtrade.prop_profile import profile_from_config

        with tempfile.TemporaryDirectory(dir=ROOT / "user_data/forensic") as temp:
            wallet = Wallets.__new__(Wallets)
            cfg = configured_account()
            cfg.update(prop_guard_enabled=True, user_data_dir=temp)
            wallet._config = cfg
            wallet._is_backtest = False
            wallet._stake_currency = "USDT"
            wallet.get_free = MagicMock(return_value=5000)
            wallet.get_total_stake_amount = MagicMock(return_value=100)
            path = Path(temp) / "risk_manager_prop_state.json"
            profile = profile_from_config(cfg)
            state = reconciled_state(profile, baseline(profile, 5000, NOW), NOW, halted=False)
            state["wake_up_lock"] = False
            with (
                patch("freqtrade.wallets.dt_now", return_value=NOW),
                patch("freqtrade.wallets.Trade.total_open_trades_stakes", return_value=40),
            ):
                self.assertEqual(wallet.get_available_stake_amount(), 0)
                path.write_text(json.dumps(state))
                self.assertEqual(wallet.get_available_stake_amount(), 60)
                for change in (
                    {"last_updated": (NOW - timedelta(seconds=16)).isoformat()},
                    {"last_updated": (NOW + timedelta(seconds=1)).isoformat()},
                    {"nuclear_triggered": True},
                    {"data_fault": True},
                    {"wake_up_lock": True},
                ):
                    path.write_text(json.dumps(state | change))
                    self.assertEqual(wallet.get_available_stake_amount(), 0)
                wallet._is_backtest = True
                self.assertEqual(wallet.get_available_stake_amount(), 60)
                wallet._is_backtest = False
                wallet._config["prop_guard_enabled"] = False
                self.assertEqual(wallet.get_available_stake_amount(), 60)

    def test_non_compounding_caps_total_stake_at_initial_balance(self):
        from freqtrade.wallets import Wallets

        wallet = Wallets.__new__(Wallets)
        wallet._stake_currency = "USDT"
        wallet._config = {
            "tradable_balance_ratio": 0.24,
            "prop_non_compounding": True,
            "prop_initial_balance": 5000,
        }
        wallet.get_free = MagicMock(return_value=6800)
        with patch("freqtrade.wallets.Trade.total_open_trades_stakes", return_value=400):
            # Even when total wallet is 7200 USDT (+2200 profit), total stake budget stays capped at 5000 * 0.24 = 1200 USDT
            self.assertEqual(wallet.get_total_stake_amount(), 1200.0)


class EquityReconstructionTests(unittest.TestCase):
    def test_dca_partial_exit_funding_and_pair_outside_original_three(self):
        import pandas as pd
        from forensic_prop_validation import equity_metrics

        with tempfile.TemporaryDirectory(dir=ROOT / "user_data/forensic") as temp:
            directory = Path(temp) / "futures"
            directory.mkdir()
            dates = pd.date_range("2024-01-01", periods=4, freq="5min", tz="UTC")
            pd.DataFrame({"date": dates, "close": [100, 80, 90, 100]}).to_feather(
                directory / "AVAX_USDT_USDT-5m-futures.feather"
            )
            stamps = [int(d.timestamp() * 1000) for d in dates]
            orders = [
                {
                    "amount": q,
                    "safe_price": price,
                    "ft_is_entry": entry,
                    "order_filled_timestamp": stamp,
                }
                for q, price, entry, stamp in zip(
                    [2, 2, 1, 3],
                    [100, 80, 90, 100],
                    [True, True, False, False],
                    stamps,
                    strict=True,
                )
            ]
            summary = {
                "starting_balance": 5000,
                "backtest_start_ts": stamps[0],
                "backtest_end_ts": stamps[-1],
                "trades": [
                    {
                        "pair": "AVAX/USDT:USDT",
                        "amount": 3,
                        "leverage": 1,
                        "is_short": False,
                        "fee_open": 0,
                        "fee_close": 0,
                        "close_timestamp": stamps[-1],
                        "profit_abs": 29,
                        "orders": orders,
                    }
                ],
            }
            result = equity_metrics(summary, temp)
            self.assertAlmostEqual(result["max_daily_dd_pct"], 0.8)
            self.assertAlmostEqual(result["max_trailing_dd_pct"], 0.8)
            self.assertAlmostEqual(result["net_profit_pct"], 0.58)
            self.assertAlmostEqual(result["max_entry_principal_committed_usdt"], 360)
            self.assertAlmostEqual(result["max_marked_exposure_usdt"], 320)

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
