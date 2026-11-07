"""P0.1 unit tests: standard library only, no exchange/client imports or network."""

import ast
import copy
import json
import math
import os
import socket
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from freqtrade.prop_capital import prop_capital_available
from freqtrade.prop_profile import floors, profile_from_config, validate_baseline
from prop_risk_guard import RiskGuard, reconciled_state

NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)


def configured_account():
    config = json.loads((ROOT / "user_data/config_prop_nfix8.json").read_text())
    config["prop_guard"].update(
        account_id="offline-account", challenge_id="offline-challenge",
        challenge_start_utc="2026-11-01T10:00:00+00:00",
        phase_start_utc="2026-11-01T10:00:00+00:00",
    )
    return config


def baseline(profile, equity=5000, now=NOW):
    return {"profile": profile, "confirmed": True, "source": "offline test evidence",
            "captured_at": now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
            "verified_at": now.isoformat(), "equity": equity}


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {}, clear=True).start()
        patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "risk_manager_prop_state.json"
        self.config = configured_account()
        self.profile = profile_from_config(self.config)
        self.api = Mock()
        self.api.get_prop_profile.return_value = self.profile
        self.api.get_equity.return_value = 5000
        self.api.get_open_trades.return_value = []
        self.api.stop_buying.return_value = True
        self.api.force_exit.return_value = True
        self.api.get_prop_control.return_value = {
            "verified": True, "flat": True, "pending_entries": False, "ack": None,
        }
        self.install()
        self.guard = self.new_guard()

    def install(self, equity=5000, now=NOW):
        self.path.write_text(json.dumps(reconciled_state(
            self.profile, baseline(self.profile, equity, now), now, halted=False)))

    def new_guard(self):
        return RiskGuard(self.api, self.path, profile=self.profile)

    def authorized(self, now=NOW, config=None):
        return prop_capital_available(self.path.parent, now, config or self.config)

    def test_official_and_operating_floors_use_initial_allocation(self):
        self.assertEqual(floors(self.profile, 5500), {
            "official_daily_floor": 5300, "official_overall_floor": 4700,
            "operating_daily_floor": 5320, "operating_overall_floor": 4740,
        })
        self.assertEqual(floors(self.profile, 4800)["operating_daily_floor"], 4620)

    def test_daily_equality_and_restart_latch(self):
        self.assertFalse(self.guard._check_drawdown(NOW))
        self.assertTrue(self.authorized())
        self.api.get_equity.return_value = 4820
        self.assertTrue(self.guard._check_drawdown(NOW + timedelta(seconds=1)))
        restored = self.new_guard()
        self.assertTrue(restored.nuclear_triggered)
        restored._check_drawdown(NOW + timedelta(seconds=2))
        self.assertTrue(restored.state["liquidation_confirmed"])
        self.assertFalse(self.authorized(NOW + timedelta(seconds=2)))
        self.api.start_trading.assert_not_called()

    def test_intraday_profit_never_raises_floor(self):
        self.api.get_equity.return_value = 5400
        self.guard._check_drawdown(NOW)
        self.api.get_equity.return_value = 5130
        self.assertFalse(self.guard._check_drawdown(NOW + timedelta(seconds=1)))
        self.assertEqual(self.guard.state["operating_daily_floor"], 4820)
        self.assertEqual(self.guard.state["operating_overall_floor"], 4740)

    def test_overall_equality_when_daily_floor_is_lower(self):
        self.install(4800)
        guard = self.new_guard()
        self.api.get_equity.return_value = 4740
        self.assertTrue(guard._check_drawdown(NOW))
        self.assertIn("overall", guard.state["halt_reason"])

    def test_restart_preserves_midnight_equity_not_current_equity(self):
        self.install(5500)
        self.api.get_equity.return_value = 5350
        guard = self.new_guard()
        self.assertFalse(guard._check_drawdown(NOW))
        restored = self.new_guard()
        self.assertEqual(restored.state["baseline"]["equity"], 5500)
        self.api.get_equity.return_value = 5320
        self.assertTrue(restored._check_drawdown(NOW + timedelta(seconds=1)))

    def test_rollover_without_baseline_blocks_but_retains_overall_check(self):
        self.guard._check_drawdown(NOW)
        tomorrow = NOW + timedelta(days=1)
        self.assertFalse(self.authorized(tomorrow))
        self.assertFalse(self.guard._check_drawdown(tomorrow))
        self.assertIsNone(self.guard.state["operating_daily_floor"])
        self.assertFalse(self.authorized(tomorrow))
        self.api.get_equity.return_value = 4740
        self.assertTrue(self.guard._check_drawdown(tomorrow + timedelta(seconds=1)))

    def test_rollover_accepts_only_explicit_current_midnight_record(self):
        tomorrow = NOW + timedelta(days=1)
        self.guard.baseline_file.write_text(json.dumps(baseline(self.profile, 5200, tomorrow)))
        self.api.get_equity.return_value = 5190
        self.assertFalse(self.guard._check_drawdown(tomorrow))
        self.assertEqual(self.guard.state["operating_daily_floor"], 5020)
        self.assertTrue(self.authorized(tomorrow))

    def test_continuous_midnight_observation_auto_certifies_and_persists_baseline(self):
        just_before = datetime(2026, 11, 5, 23, 59, 57, tzinfo=UTC)
        just_after = datetime(2026, 11, 6, 0, 0, 2, tzinfo=UTC)
        self.api.get_equity.return_value = 5150
        self.assertFalse(self.guard._check_drawdown(just_before))
        self.assertTrue(self.authorized(just_before))
        self.api.get_equity.return_value = 5160
        self.assertFalse(self.guard._check_drawdown(just_after))
        self.assertFalse(self.guard.state["data_fault"])
        self.assertEqual(self.guard.state["baseline"]["equity"], 5160)
        self.assertEqual(self.guard.state["baseline"]["captured_at"], "2026-11-06T00:00:00+00:00")
        self.assertEqual(self.guard.state["operating_daily_floor"], 4980)
        persisted = json.loads(self.guard.baseline_file.read_text(encoding="utf-8"))
        self.assertEqual(persisted["equity"], 5160)
        self.assertEqual(persisted["captured_at"], "2026-11-06T00:00:00+00:00")
        self.assertTrue(self.authorized(just_after))

    def test_missing_state_never_bootstraps_even_with_baseline_file(self):
        self.path.unlink()
        self.guard.baseline_file.write_text(json.dumps(baseline(self.profile)))
        for equity in (5000, 4990, 5350):
            guard = self.new_guard()
            self.api.get_equity.return_value = equity
            guard._check_drawdown(NOW)
            self.assertIsNotNone(guard.state_error)
            self.assertFalse(self.path.exists())
        self.api.get_equity.assert_not_called()

    def test_legacy_corrupt_and_wrong_identity_preserved(self):
        original = json.loads(self.path.read_text())
        wrong = copy.deepcopy(original)
        wrong["profile"]["account_id"] = "other"
        for content in ("{broken", json.dumps({"schema_version": 1}), json.dumps(wrong), "[]"):
            self.path.write_text(content)
            guard = self.new_guard()
            guard._check_drawdown(NOW)
            self.assertIsNotNone(guard.state_error)
            self.assertEqual(self.path.read_text(), content)
            self.assertFalse(self.authorized())

    def test_invalid_equity_locks_entries(self):
        for bad in (None, math.nan, math.inf, -math.inf, True, "5000"):
            self.api.get_equity.return_value = bad
            self.guard._check_drawdown(NOW)
            self.assertFalse(self.authorized())
            self.assertTrue(self.guard.state["data_fault"])

    def test_zero_and_negative_equity_liquidate(self):
        for value in (0, -1):
            self.install()
            guard = self.new_guard()
            self.api.get_equity.return_value = value
            self.assertTrue(guard._check_drawdown(NOW))

    def test_unknown_liquidation_is_not_confirmed(self):
        self.api.get_open_trades.return_value = None
        self.guard.trigger_nuclear_option("test")
        self.assertFalse(self.guard.state["liquidation_confirmed"])
        self.api.get_open_trades.return_value = []
        self.guard._check_drawdown(NOW)
        self.assertTrue(self.guard.state["liquidation_confirmed"])

    def test_write_failure_preserves_previous_record(self):
        original = self.path.read_text()
        with patch("prop_risk_guard.os.replace", side_effect=OSError("disk full")):
            self.guard._check_drawdown(NOW)
        self.assertIsNotNone(self.guard.state_error)
        self.assertEqual(self.path.read_text(), original)
        self.api.stop_buying.assert_called()

    def test_config_mismatch_and_unavailable_rpc_never_authorize(self):
        for remote in (None, self.profile | {"initial_balance": 25000}):
            self.install()
            guard = self.new_guard()
            self.api.get_prop_profile.return_value = remote
            guard._check_drawdown(NOW)
            self.assertIsNotNone(guard.state_error)
            self.assertFalse(self.authorized())
        self.api.get_equity.assert_not_called()

    def test_wallet_rejects_old_schema_wrong_config_stale_and_future_state(self):
        self.guard._check_drawdown(NOW)
        changed = copy.deepcopy(self.config)
        changed["prop_guard"]["challenge_id"] = "another"
        self.assertFalse(self.authorized(config=changed))
        self.assertFalse(self.authorized(NOW + timedelta(seconds=16)))
        self.assertFalse(self.authorized(NOW - timedelta(seconds=1)))
        state = json.loads(self.path.read_text())
        state.pop("risk_schema_version")
        self.path.write_text(json.dumps(state))
        self.assertFalse(self.authorized())

    def test_clock_rollback_locks_without_rebasing(self):
        self.guard._check_drawdown(NOW - timedelta(seconds=1))
        self.assertIsNotNone(self.guard.state_error)
        self.assertEqual(self.guard.state["baseline"]["equity"], 5000)

    def test_invalid_or_incomplete_profile_rejected(self):
        raw = json.loads((ROOT / "user_data/config_prop_nfix8.json").read_text())
        with self.assertRaises(ValueError):
            profile_from_config(raw)
        for key, value in (("daily_limit_percent", .04), ("max_loss_percent", .06),
                           ("official_daily_limit_percent", .05), ("account_id", ""),
                           ("phase_id", "funded"), ("daily_limit_percent", math.nan)):
            changed = copy.deepcopy(self.config)
            changed["prop_guard"][key] = value
            with self.assertRaises(ValueError):
                profile_from_config(changed)
        for key in ("prop_initial_balance",):
            changed = copy.deepcopy(self.config)
            changed[key] = True
            with self.assertRaises(ValueError):
                profile_from_config(changed)

    def test_legacy_env_must_agree_with_resolved_config(self):
        with patch.dict(os.environ, {"GLOBAL_HARD_STOPLOSS_PERCENT": "0.04"}):
            with self.assertRaises(ValueError):
                profile_from_config(self.config)
        with patch.dict(os.environ, {"GLOBAL_HARD_STOPLOSS_PERCENT": "0.036"}):
            self.assertEqual(profile_from_config(self.config), self.profile)
        with patch.dict(os.environ, {"GLOBAL_HARD_STOPLOSS_PERCENT": "invalid"}):
            with self.assertRaises(ValueError):
                profile_from_config(self.config)

    def test_commissioning_preserves_explicit_halt_and_unsafe_interval_rejected(self):
        state = reconciled_state(self.profile, baseline(self.profile), NOW, halted=True)
        self.path.write_text(json.dumps(state))
        guard = self.new_guard()
        self.assertTrue(guard.nuclear_triggered)
        guard._check_drawdown(NOW)
        self.assertFalse(self.authorized())
        for interval in (0, 30, True, math.nan):
            with self.assertRaises(ValueError):
                RiskGuard(self.api, self.path, profile=self.profile, interval=interval)

    def test_baseline_requires_identity_midnight_evidence_and_utc(self):
        for change in ({"confirmed": False}, {"equity": math.nan}, {"source": ""},
                       {"captured_at": NOW.isoformat()},
                       {"captured_at": "2026-11-05T00:00:00"},
                       {"verified_at": (NOW + timedelta(seconds=1)).isoformat()},
                       {"profile": self.profile | {"challenge_id": "other"}}):
            with self.assertRaises((ValueError, TypeError)):
                validate_baseline(baseline(self.profile) | change, self.profile, NOW)
        b = baseline(self.profile) | {"captured_at": "2026-11-05T01:00:00+01:00"}
        validate_baseline(b, self.profile, NOW)

    def test_nonrisk_workers_not_used_for_drawdown(self):
        self.guard._check_drawdown(NOW)
        self.api.get_trades.assert_not_called()
        self.api.get_open_trades.assert_not_called()

    def test_rpc_exports_resolved_profile_without_credentials(self):
        # Execute only the pure show_config function, not RPC/exchange initialization.
        tree = ast.parse((ROOT / "freqtrade/rpc/rpc.py").read_text())
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                    and n.name == "_rpc_show_config")
        node.decorator_list = []
        namespace = {"__version__": "offline", "decimals_per_coin": lambda _: 8,
                     "timeframe_to_msecs": lambda _: 300000,
                     "timeframe_to_minutes": lambda _: 5}
        code = "from __future__ import annotations\n" + ast.unparse(node)
        exec(compile(code, "offline_show_config", "exec"), namespace)
        config = self.config | {"runmode": Mock(value="live")}
        config["exchange"] = config["exchange"] | {"key": "SECRET", "secret": "SECRET"}
        result = namespace["_rpc_show_config"](config, "running")
        self.assertEqual(result["prop_profile"], self.profile)
        self.assertNotIn("SECRET", json.dumps(result))
        config["prop_initial_balance"] = 25000
        self.assertEqual(namespace["_rpc_show_config"](config, "running")["prop_profile"]["initial_balance"], 25000)


if __name__ == "__main__":
    unittest.main()
