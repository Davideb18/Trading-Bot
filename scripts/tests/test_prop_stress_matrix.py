"""Stress-testing and failure mode simulation matrix for HyroTrader One-Step.

Simulates 7 critical production failure scenarios:
1. Flash Crash & Floor Breach Execution (P0.5 Emergency Liquidation)
2. Network Timeout & Dispatch State Crash Gap (P0.3 Idempotency)
3. SIGKILL / Abrupt Process Death in Sending State
4. Partial Fill Race Condition on Cancel & Replace Order
5. Midnight Rollover Downtime Gap (P0.1 Fail-Closed Baseline Guard)
6. Out-of-band / Manual Exchange Trade Detection (Findings Latch)
7. 24-Day Inactivity Sentinel Verification (No automated forceentry)
"""

import copy
import hashlib
import json
import logging
import math
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from freqtrade.emergency_authority import EmergencyTakeover, require_core_authority
from freqtrade.emergency_supervisor import EmergencySupervisor, UnverifiedAccount
from freqtrade.exceptions import ExchangeError
from freqtrade.journal_execution import bind_journal, journal_create_order, open_journal, reconcile_journal
from freqtrade.order_journal import JournalBlocked, OrderJournal
from freqtrade.order_recovery import clear_recovery, mark_recovery, pending_recovery
from freqtrade.prop_controls import supervisor_state, sync_prop_controls
from freqtrade.prop_profile import floors, profile_from_config, validate_baseline, validate_profile
from freqtrade.prop_risk_budget import budget, inventory, require_risk_budget
from prop_risk_guard import RiskGuard, reconciled_state


PAIR = "BTC/USDT:USDT"
REQUEST = {
    "pair": PAIR,
    "side": "buy",
    "type": "limit",
    "amount": 0.05,
    "price": 2000.0,
    "reduceOnly": False,
}


def base_profile(now=None):
    now = now or datetime.now(UTC)
    mid = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "profile_id": "hyro_one_step",
        "account_id": "stress-acc-1",
        "challenge_id": "stress-ch-1",
        "phase_id": "evaluation",
        "challenge_start_utc": mid.isoformat(),
        "phase_start_utc": mid.isoformat(),
        "official_daily_limit_percent": 0.04,
        "official_max_loss_percent": 0.06,
        "daily_limit_percent": 0.036,
        "max_loss_percent": 0.052,
        "profit_target_percent": 0.10,
        "daily_profit_cap_percent": 0.40,
        "min_trading_days": 5,
        "initial_balance": 5000.0,
        "exchange": "bybit",
        "environment": "demo",
        "currency": "USDT",
    }


def make_baseline(profile, equity=5000.0, now=None):
    now = now or datetime.now(UTC)
    mid = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "profile": profile,
        "equity": float(equity),
        "captured_at": mid.isoformat(),
        "verified_at": now.isoformat(),
        "source": "bybit_wallet_coin_equity",
        "confirmed": True,
        "status": "verified",
        "evidence": {"kind": "historical_account_equity", "reviewed": True,
                     "account_id": profile["account_id"], "reference": "offline-fixture",
                     "equity": float(equity), "observed_at": mid.isoformat()},
    }


def make_snapshot(*, positions=None, open_orders=None, orders=None, fills=None):
    return {
        "complete": True,
        "positions": list(positions or []),
        "open_orders": list(open_orders or []),
        "orders": dict(orders or {}),
        "fills": dict(fills or {}),
        "checked_ms": int(time.time() * 1000),
    }


class StressMatrixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.now = datetime.now(UTC)
        self.profile = base_profile(self.now)
        self.api_key = "stress-test-raw-key-12345"
        self.identity = {k: self.profile[k] for k in (
            "profile_id", "account_id", "challenge_id", "phase_id", "exchange", "environment", "currency"
        )}
        self.identity["key_fingerprint"] = hashlib.sha256(self.api_key.encode()).hexdigest()
        self.journal_dir = self.tmp / "order_journal"
        self.journal = OrderJournal(self.journal_dir, self.identity)
        self.state_file = self.tmp / "risk_manager_prop_state.json"
        self.baseline_file = self.tmp / "risk_manager_prop_baseline.json"
        self.baseline = make_baseline(self.profile, 5000.0, self.now)
        self.state = reconciled_state(self.profile, self.baseline, self.now, halted=False)
        self.state["data_valid"] = True
        self.state["wake_up_lock"] = False
        self.state["remote_identity_verified"] = True
        self.save_state()
        self.save_baseline()

        self.bot = SimpleNamespace()
        self.bot.order_journal = self.journal
        self.bot.config = {
            "prop_guard_enabled": True,
            "prop_non_compounding": True,
            "trading_mode": "futures",
            "margin_mode": "isolated",
            "dry_run": False,
            "stake_currency": "USDT",
            "prop_initial_balance": 5000.0,
            "user_data_dir": str(self.tmp),
            "exchange": {"name": "bybit", "demo_trading": True, "key": self.api_key, "secret": "s"},
            "tradable_balance_ratio": 0.2,
            "prop_guard": {
                "profile_id": "hyro_one_step",
                "account_id": "stress-acc-1",
                "challenge_id": "stress-ch-1",
                "phase_id": "evaluation",
                "challenge_start_utc": self.profile["challenge_start_utc"],
                "phase_start_utc": self.profile["phase_start_utc"],
                "official_daily_limit_percent": 0.04,
                "official_max_loss_percent": 0.06,
                "daily_limit_percent": 0.036,
                "max_loss_percent": 0.052,
                "profit_target_percent": 0.10,
                "daily_profit_cap_percent": 0.40,
                "min_trading_days": 5,
            },
        }

    def refresh_state_timestamp(self):
        self.now = datetime.now(UTC)
        self.state["last_updated"] = self.now.isoformat()
        self.save_state()

    def save_state(self):
        self.state_file.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def save_baseline(self):
        self.baseline_file.write_text(json.dumps(self.baseline, indent=2), encoding="utf-8")

    def tearDown(self):
        self.journal.close()

    # -------------------------------------------------------------------------
    # Scenario 1: Flash Crash & Emergency Floor Breach
    # -------------------------------------------------------------------------
    def test_scenario_1_flash_crash_triggers_emergency_takeover_and_flat_proof(self):
        """When equity plunges past operating floor, supervisor takes over, shuts down core, and liquidates."""
        self.refresh_state_timestamp()
        api = Mock()
        api.get_prop_profile.return_value = self.profile
        api.get_equity.return_value = 4735.0  # Breach: 5000 - 4735 = 265 USDT > 260 USDT max loss limit (5.2%)
        api.get_prop_control.return_value = {"verified": True, "flat": False, "pending_entries": False}
        api.stop_buying.return_value = True

        guard = RiskGuard(api, self.state_file, profile=self.profile, baseline_file=self.baseline_file)
        triggered = guard._check_drawdown(now=self.now)
        self.assertTrue(triggered)
        self.assertTrue(guard.nuclear_triggered)

        # In journal, write emergency state requesting handoff
        with self.journal.db:
            self.journal.db.execute("INSERT OR REPLACE INTO emergency VALUES (1, ?)", (
                json.dumps({
                    "owner": "sup-liquidator-1",
                    "handoff": True,
                    "phase": "liquidating",
                    "halt_id": "halt-flash-crash-1",
                }),
            ))

        # Core bot checks authority during trading loop -> must immediately exit with EmergencyTakeover
        with self.assertRaises(EmergencyTakeover):
            require_core_authority(self.bot)
        self.assertTrue(getattr(self.bot, "_emergency_handoff", False))

        # Core exits and releases journal lock. Emergency supervisor takes over.
        self.journal.close()
        adapter = Mock()
        adapter.operation_budget = lambda s: nullcontext()
        adapter.snapshot.side_effect = [
            # Snapshot 1: 1 open entry to cancel, 1 open position
            {
                "checked_ms": int(time.time() * 1000),
                "orders": [{"id": "ord-entry-1", "clientOrderId": "ftj-entry-1", "symbol": PAIR, "reduceOnly": False, "side": "buy"}],
                "positions": [{"pair": PAIR, "symbol": PAIR, "amount": 0.5, "side": "buy"}],
            },
            # Snapshot 2: order cancelled, position remains
            {
                "checked_ms": int(time.time() * 1000) + 100,
                "orders": [],
                "positions": [{"pair": PAIR, "symbol": PAIR, "amount": 0.5, "side": "buy"}],
            },
            # Snapshot 3: after position market liquidation -> first flat snapshot
            {
                "checked_ms": int(time.time() * 1000) + 200,
                "orders": [],
                "positions": [],
            },
            # Snapshot 4: double-check flat confirmation
            {
                "checked_ms": int(time.time() * 1000) + 300,
                "orders": [],
                "positions": [],
            },
        ]
        adapter.expected_amount.return_value = 0.5
        adapter.lookup.return_value = {
            "id": "ord-entry-1", "clientOrderId": "ftj-entry-1", "symbol": PAIR,
            "side": "buy", "reduceOnly": False, "amount": 0.5, "filled": 0.0,
            "status": "canceled", "price": 2000.0,
        }
        adapter.close_request.return_value = {
            "pair": PAIR, "side": "sell", "ordertype": "market", "amount": 0.5, "reduceOnly": True,
        }
        adapter.create_order.return_value = {"id": "liq-exit-1", "clientOrderId": "ftj-liq-1"}

        sup = EmergencySupervisor(adapter, self.journal_dir, self.state_file, self.profile, self.api_key, coordinated=True)
        # Pre-seed intent in SQLite so cancellation knows it
        with sup.connection() as conn:
            conn.execute("INSERT INTO intents(client_id,created_ms,request,state) VALUES (?,?,?,?)",
                         ("ftj-entry-1", int(time.time() * 1000), json.dumps(REQUEST), "accepted"))

        # Step 1: cancel open entries
        confirmed = sup.step()
        self.assertFalse(confirmed)

        # Settle the orders in DB
        with sup.connection() as conn:
            conn.execute("UPDATE intents SET settled=1 WHERE client_id LIKE 'ftj%'")

        # Step 2: double flat verification
        confirmed = sup.step()
        self.assertTrue(confirmed)
        sup.close()

    # -------------------------------------------------------------------------
    # Scenario 2: Network Timeout & Dispatch State Crash Gap
    # -------------------------------------------------------------------------
    def test_scenario_2_network_timeout_marks_uncertain_and_blocks_until_reconciled(self):
        """When create_order times out, state becomes uncertain and gate strictly blocks any new entries."""
        self.refresh_state_timestamp()
        self.journal.reconcile(make_snapshot(), {}, {})
        self.assertTrue(self.journal.ready)

        cid = self.journal.prepare(REQUEST, trade_id=10)

        def failing_create(**kwargs):
            raise TimeoutError("Bybit API connection timeout (504)")

        with self.assertRaises(TimeoutError):
            self.journal.dispatch(cid, failing_create)

        row = self.journal.db.execute("SELECT state, settled FROM intents WHERE client_id=?", (cid,)).fetchone()
        self.assertEqual(row["state"], "uncertain")
        self.assertEqual(row["settled"], 0)

        with self.assertRaisesRegex(JournalBlocked, "Stale or unverified reconciliation|unobserved_reservations_pending"):
            require_risk_budget(self.bot, REQUEST, trade_id=11)

    # -------------------------------------------------------------------------
    # Scenario 3: Abrupt Process Death (SIGKILL) in Sending State
    # -------------------------------------------------------------------------
    def test_scenario_3_process_death_in_sending_state_never_retried_and_blocks(self):
        """An intent killed in 'sending' state must survive restart, cannot be re-dispatched, and blocks new entries."""
        self.refresh_state_timestamp()
        self.journal.reconcile(make_snapshot(), {}, {})
        cid = self.journal.prepare(REQUEST, trade_id=20)

        with self.journal.db:
            self.journal.db.execute("UPDATE intents SET state='sending' WHERE client_id=?", (cid,))

        self.journal.close()

        restarted_journal = OrderJournal(self.journal_dir, self.identity)
        self.addCleanup(restarted_journal.close)

        with self.assertRaisesRegex(JournalBlocked, "Intent already dispatched"):
            restarted_journal.dispatch(cid, lambda **kw: None)

        # 2. Before reconciliation against exchange, new entry is strictly blocked
        self.bot.order_journal = restarted_journal
        with self.assertRaisesRegex(JournalBlocked, "Stale or unverified reconciliation|reconciliation required"):
            require_risk_budget(self.bot, REQUEST, trade_id=21)

        # 3. If exchange has no record of the sending order, reconcile reports it unresolved
        errors = restarted_journal.reconcile(make_snapshot(), {}, {})
        self.assertIn("unresolved:" + cid, errors)
        self.assertFalse(restarted_journal.ready)

        # 4. Entry remains strictly blocked
        with self.assertRaisesRegex(JournalBlocked, "Stale or unverified reconciliation|reconciliation required"):
            require_risk_budget(self.bot, REQUEST, trade_id=21)

    # -------------------------------------------------------------------------
    # Scenario 4: Partial Fill Race Condition during Order Replacement
    # -------------------------------------------------------------------------
    def test_scenario_4_partial_fill_during_cancel_assigned_to_position(self):
        """If 0.02 of 0.05 is executed while cancellation is in flight, reconciliation correctly attributes 0.02 to position."""
        self.refresh_state_timestamp()
        self.journal.reconcile(make_snapshot(), {}, {})
        cid = self.journal.prepare(REQUEST, trade_id=30)
        order = self.journal.dispatch(
            cid, lambda **kw: {"id": "bybit-ord-40", "clientOrderId": cid, "symbol": PAIR, "side": "buy"}
        )

        snapshot = make_snapshot(
            positions=[{"symbol": PAIR, "side": "long", "amount": 0.02, "mark_price": 2000.0}],
            open_orders=[],
            orders={
                cid: {
                    "id": "bybit-ord-40", "clientOrderId": cid, "symbol": PAIR, "side": "buy",
                    "amount": 0.05, "filled": 0.02, "remaining": 0.0, "status": "canceled",
                    "average": 2000.0,
                }
            },
            fills={cid: 0.02},
        )
        local_orders = {
            "bybit-ord-40": {
                "symbol": PAIR, "side": "buy", "amount": 0.05, "filled": 0.02,
                "status": "canceled", "trade_id": 30,
            }
        }
        local_positions = {(PAIR, "long"): 0.02}

        errors = self.journal.reconcile(snapshot, local_orders, local_positions)
        self.assertEqual(errors, [])
        self.assertTrue(self.journal.ready)

        row = self.journal.db.execute("SELECT state, settled, linked FROM intents WHERE client_id=?", (cid,)).fetchone()
        self.assertEqual(row["state"], "canceled")
        self.assertEqual(row["settled"], 1)
        self.assertEqual(row["linked"], 1)

        inv = inventory(self.journal)
        self.assertEqual(inv["unobserved_reservations"], [])
        self.assertEqual(len(inv["positions"]), 1)
        self.assertEqual(inv["positions"][0]["amount"], 0.02)

    # -------------------------------------------------------------------------
    # Scenario 5: Midnight Rollover Downtime Gap
    # -------------------------------------------------------------------------
    def test_scenario_5_midnight_downtime_gap_refuses_auto_rollover_and_fails_closed(self):
        """If the bot was down between 23:59:10 and 00:00:45 UTC (>15s window), midnight rollover fails closed."""
        api = Mock()
        api.get_prop_profile.return_value = self.profile
        api.get_equity.return_value = 5020.0
        guard = RiskGuard(api, self.state_file, profile=self.profile, baseline_file=self.baseline_file, interval=5)

        prev_now = datetime(2026, 11, 7, 23, 58, 0, tzinfo=UTC)
        guard.state["last_updated"] = prev_now.isoformat()
        guard.state["baseline"] = self.baseline
        guard.state["data_fault"] = False
        guard.state["nuclear_triggered"] = False

        now_d = datetime(2026, 11, 8, 0, 2, 0, tzinfo=UTC)
        guard._check_drawdown(now_d)
        self.assertTrue(guard.state["data_fault"])
        self.assertFalse(guard.state["data_valid"])
        self.assertIsNone(guard.state["baseline"])

    # -------------------------------------------------------------------------
    # Scenario 6: Out-of-band / Manual Exchange Trade Detection
    # -------------------------------------------------------------------------
    def test_scenario_6_external_trade_latches_findings_and_blocks_entries(self):
        """When an external trade or position appears on Bybit without local journal origin, journal locks."""
        self.refresh_state_timestamp()
        self.journal.reconcile(make_snapshot(), {}, {})
        self.assertTrue(self.journal.ready)

        rogue_snapshot = make_snapshot(
            positions=[{"symbol": "ETH/USDT:USDT", "side": "long", "amount": 1.5, "mark_price": 3000.0}],
            open_orders=[],
            orders={},
            fills={},
        )
        local_orders = {}
        local_positions = {}

        errors = self.journal.reconcile(rogue_snapshot, local_orders, local_positions)
        self.assertIn("position_mismatch:ETH/USDT:USDT", errors)
        self.assertIn("external_position:ETH/USDT:USDT:long", errors)
        self.assertFalse(self.journal.ready)

        finding = self.journal.db.execute("SELECT detail FROM findings WHERE key LIKE 'external_position:%'").fetchone()
        self.assertIn("Position without local trade or journal origin", finding[0])

        model = SimpleNamespace(get_trades_proxy=lambda: [], commit=Mock())
        self.bot.exchange = SimpleNamespace(fetch_journal_snapshot=Mock(return_value=rogue_snapshot))
        self.bot.has_unresolved_orders = lambda: False
        with self.assertRaises(JournalBlocked):
            journal_create_order(self.bot, None, model, **REQUEST)

    # -------------------------------------------------------------------------
    # Scenario 7: Inactivity Sentinel Alert (No Illegal Force-Entry)
    # -------------------------------------------------------------------------
    def test_scenario_7_inactivity_24_days_emits_critical_alert_without_orders(self):
        """At 24 days of flat inactivity, Sentinel logs critical alert without invoking forceentry."""
        from llm_risk_manager_prop import RiskManager
        mgr = SimpleNamespace(
            _shutdown=SimpleNamespace(is_set=Mock(side_effect=[False, True]), wait=Mock()),
            rules_api=SimpleNamespace(
                get_open_trades=lambda: [],
                get_trades=lambda **kw: [
                    {
                        "close_timestamp": int((datetime.now(UTC) - timedelta(days=25)).timestamp() * 1000),
                        "profit_abs": 20.0,
                        "cost": 300.0,
                        "profit_ratio": 0.05,
                    }
                ],
                post=Mock(),
            ),
            nuclear_triggered=False,
            initial_balance=5000.0,
            profile={"profit_target_percent": 0.10, "daily_profit_cap_percent": 0.40, "min_trading_days": 5},
            request_data_fault=Mock(),
        )

        with self.assertLogs("HyroTrader.RiskManagerProp", level="CRITICAL") as log:
            RiskManager._hyrotrader_rules_loop(mgr)

        self.assertTrue(any("24-DAY INACTIVITY ALERT" in line for line in log.output))
        mgr.rules_api.post.assert_not_called()
        mgr.request_data_fault.assert_not_called()


if __name__ == "__main__":
    unittest.main()
