"""Integration test verifying NostalgiaForInfinityX8_Prop capital budgeting,
DCA/grinding capacity, and risk supervisor interlocks for HyroTrader One-Step."""

import json
import math
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

from freqtrade.enums import RunMode
from freqtrade.exchange.bybit import Bybit
from freqtrade.order_journal import OrderJournal
from freqtrade.prop_capital import prop_capital_available
from freqtrade.prop_profile import floors, profile_from_config
from freqtrade.snapshot_freshness import validate_risk_reads
from user_data.strategies.NostalgiaForInfinityX8_Prop import NostalgiaForInfinityX8_Prop


class PropNfix8IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.user_dir = Path(self.tmp.name)
        self.strategy = NostalgiaForInfinityX8_Prop(config={
            "runmode": RunMode.DRY_RUN,
            "user_data_dir": self.user_dir,
            "trading_mode": "futures",
            "margin_mode": "isolated",
            "max_open_trades": 4,
            "tradable_balance_ratio": 0.20,
            "stake_currency": "USDT",
            "prop_guard_enabled": True,
            "exchange": {"name": "bybit"},
            "order_types": {"stoploss": "market", "force_exit": "market"},
            "stoploss": -0.99,
        })
        self.strategy.dp = Mock()

    def tearDown(self):
        self.tmp.cleanup()

    def test_capital_allocation_guarantees_dca_headroom(self):
        """3 concurrent slots at 1.0x native stake yield ~$333.33 (> $250 qualifying floor) within $1,000 cap."""
        total_account = 5000.0
        tradable_ratio = 0.20
        max_trades = 3
        total_tradable = total_account * tradable_ratio  # 1000.0 USDT
        slot_budget = total_tradable / max_trades       # 333.333... USDT

        # Test initial stake for regular / top_coins / scalp mode
        initial_stake = self.strategy.custom_stake_amount(
            pair="BTC/USDT:USDT",
            current_time=datetime.now(UTC),
            current_rate=50000.0,
            proposed_stake=slot_budget,
            min_stake=5.0,
            max_stake=666.0,
            leverage=1.0,
            entry_tag="1",  # normal mode tag
            side="long",
        )
        self.assertAlmostEqual(initial_stake, slot_budget, places=6, msg="Regular entry must use native 1.0x slot budget (~333.33 USDT)")
        self.assertGreaterEqual(initial_stake, 250.0, "Regular entry must satisfy HyroTrader $250 qualifying trade notional floor")

        # 3 concurrent regular trades cap strictly at $1,000 aggregate notional (20% rule)
        total_used_initial = initial_stake * max_trades
        self.assertAlmostEqual(total_used_initial, 1000.0, places=6)

        # With 1-2 open trades, remaining headroom accommodates rebuy (25%) and grind (20%) DCA entries
        rebuy_stake = self.strategy.custom_stake_amount(
            pair="ETH/USDT:USDT",
            current_time=datetime.now(UTC),
            current_rate=3000.0,
            proposed_stake=slot_budget,
            min_stake=5.0,
            max_stake=666.0,
            leverage=1.0,
            entry_tag="61",  # rebuy mode tag
            side="long",
        )
        self.assertAlmostEqual(rebuy_stake, slot_budget * 0.25, places=6)

        # Test grind mode entry
        grind_stake = self.strategy.custom_stake_amount(
            pair="SOL/USDT:USDT",
            current_time=datetime.now(UTC),
            current_rate=150.0,
            proposed_stake=slot_budget,
            min_stake=5.0,
            max_stake=666.0,
            leverage=1.0,
            entry_tag="120",  # grind mode tag
            side="long",
        )
        self.assertAlmostEqual(grind_stake, slot_budget * 0.20, places=6)

    def test_confirm_trade_entry_interlocks(self):
        """Strategy gate accepts fresh valid supervisor state and rejects all fault modes."""
        state_file = self.user_dir / "risk_manager_prop_state.json"
        now = datetime.now(UTC)

        valid_state = {
            "schema_version": 1,
            "risk_schema_version": 2,
            "control_schema_version": 1,
            "last_updated": now.isoformat(),
            "nuclear_triggered": False,
            "data_fault": False,
            "wake_up_lock": False,
            "macro_paused": False,
            "data_valid": True,
        }

        # 1. Valid state -> allowed
        state_file.write_text(json.dumps(valid_state))
        self.assertTrue(
            self.strategy.confirm_trade_entry("BTC/USDT:USDT", "limit", 1.0, 50000.0, "GTC", now, "force_entry", "long"),
            "Entry should be confirmed when supervisor state is healthy",
        )

        # 2. Reject short entries
        self.assertFalse(
            self.strategy.confirm_trade_entry("BTC/USDT:USDT", "limit", 1.0, 50000.0, "GTC", now, "force_entry", "short"),
            "Short entries must be unconditionally rejected for One-Step profile",
        )

        # 3. Reject when data_fault is True
        state_file.write_text(json.dumps({**valid_state, "data_fault": True}))
        self.assertFalse(
            self.strategy.confirm_trade_entry("BTC/USDT:USDT", "limit", 1.0, 50000.0, "GTC", now, "force_entry", "long")
        )

        # 4. Reject when macro_paused is True
        state_file.write_text(json.dumps({**valid_state, "macro_paused": True}))
        self.assertFalse(
            self.strategy.confirm_trade_entry("BTC/USDT:USDT", "limit", 1.0, 50000.0, "GTC", now, "force_entry", "long")
        )

        # 5. Reject when nuclear_triggered is True
        state_file.write_text(json.dumps({**valid_state, "nuclear_triggered": True}))
        self.assertFalse(
            self.strategy.confirm_trade_entry("BTC/USDT:USDT", "limit", 1.0, 50000.0, "GTC", now, "force_entry", "long")
        )

        # 6. Reject when state is stale (> 15 seconds)
        stale_time = (now - timedelta(seconds=20)).isoformat()
        state_file.write_text(json.dumps({**valid_state, "last_updated": stale_time}))
        self.assertFalse(
            self.strategy.confirm_trade_entry("BTC/USDT:USDT", "limit", 1.0, 50000.0, "GTC", now, "force_entry", "long")
        )

    def test_bybit_snapshot_with_observed_mark_price_passes_validation(self):
        """Positions parsed from Bybit contain HTTP observation timestamp and satisfy risk reads."""
        now_ms = time.time() * 1000
        reads = [
            {"component": "open_orders", "started_ms": now_ms - 200, "received_ms": now_ms - 150, "server_ms": now_ms - 170, "elapsed_ms": 50},
            {"component": "positions", "started_ms": now_ms - 150, "received_ms": now_ms - 100, "server_ms": now_ms - 120, "elapsed_ms": 50},
            {"component": "open_orders_end", "started_ms": now_ms - 100, "received_ms": now_ms - 60, "server_ms": now_ms - 80, "elapsed_ms": 40},
            {"component": "positions_end", "started_ms": now_ms - 60, "received_ms": now_ms - 20, "server_ms": now_ms - 40, "elapsed_ms": 40},
        ]
        pos_read_ms = next(r["received_ms"] for r in reversed(reads) if r["component"] == "positions")

        snapshot = {
            "complete": True,
            "open_orders": [],
            "orders": {},
            "fills": {},
            "reads": reads,
            "positions": [{
                "symbol": "BTC/USDT:USDT",
                "side": "long",
                "amount": 0.05,
                "mark_price": 60000.0,
                "mark_price_observed_ms": pos_read_ms,
            }],
        }

        state_reads = [
            {"component": "identity", "started_ms": now_ms - 50, "received_ms": now_ms - 30, "server_ms": now_ms - 40, "elapsed_ms": 20},
            {"component": "account_mode", "started_ms": now_ms - 30, "received_ms": now_ms - 15, "server_ms": now_ms - 20, "elapsed_ms": 15},
            {"component": "equity", "started_ms": now_ms - 15, "received_ms": now_ms - 5, "server_ms": now_ms - 10, "elapsed_ms": 10},
        ]
        state = {"risk_component_reads": state_reads}

        # Must validate cleanly without raising ValueError("Unverified mark price observation time")
        validate_risk_reads(snapshot, state, now=now_ms)
        self.assertTrue(True, "Snapshot with populated mark_price_observed_ms must validate")


if __name__ == "__main__":
    unittest.main()
