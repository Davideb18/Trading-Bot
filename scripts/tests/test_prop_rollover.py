"""UTC rollover proofs: real guard and independent adapter, offline exchange doubles."""
import json
import socket
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
from prop_risk_guard import RiskGuard, reconciled_state
from prop_independent import IndependentRiskAPI
from freqtrade.prop_profile import profile_from_config
from freqtrade.prop_capital import prop_capital_available
from freqtrade.order_journal import OrderJournal
from test_prop_profile import configured_account, baseline


class RolloverTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(socket.socket, 'connect', side_effect=AssertionError('Network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = configured_account()
        self.config['user_data_dir'] = self.root
        self.profile = profile_from_config(self.config)
        self.before = datetime.now(UTC).replace(hour=23, minute=59, second=59, microsecond=0)
        self.after = self.before + timedelta(seconds=2)
        self.path = self.root / 'risk_manager_prop_state.json'
        self.path.write_text(json.dumps(reconciled_state(
            self.profile, baseline(self.profile, now=self.before), self.before, halted=False)))
        identity = {k: self.profile[k] for k in ('profile_id', 'account_id', 'challenge_id',
                                                'phase_id', 'exchange', 'environment', 'currency')}
        self.journal = OrderJournal(self.root / 'order_journal', identity)
        self.addCleanup(self.journal.close)
        self.direct = Mock()
        self.direct.verify_identity.return_value = self.profile
        self.direct.equity.return_value = 5000.
        self.direct.equity_observation = None
        self.direct.snapshot.return_value = {'positions': [{'amount': 1}], 'orders': []}
        self.control = Mock()
        self.control.get_prop_control.return_value = dict(verified=True, flat=False, pending_entries=False, ack=None)
        self.api = IndependentRiskAPI(self.direct, self.control)
        self.emergency = Mock()
        self.emergency.step.return_value = False
        self.guard = RiskGuard(self.api, self.path, profile=self.profile, emergency=self.emergency)

    def rollover(self):
        self.guard._check_drawdown(self.before)
        self.guard._check_drawdown(self.after)

    def test_overnight_position_explicit_persistent_daily_fault(self):
        self.rollover()
        state = json.loads(self.path.read_text())
        self.assertEqual(state['overnight_exposure']['status'], 'present')
        self.assertFalse(state['daily_monitoring_available'])
        self.assertTrue(state['overnight_policy_required'])
        self.assertEqual(state['monitoring_status'], 'overall_only')
        self.assertIsNone(state['operating_daily_floor'])
        self.assertTrue(state['data_fault'])
        self.assertFalse(prop_capital_available(self.root, self.after, self.config))
        events = [r[0] for r in self.journal.db.execute('SELECT reason FROM fault_events')]
        self.assertTrue(any('Daily floor unverifiable; exposure=present' in e for e in events))
        self.emergency.step.assert_not_called()

    def test_pending_order_alone_is_exposure(self):
        self.direct.snapshot.return_value = {'positions': [], 'orders': [{'id': 'pending'}]}
        self.rollover()
        self.assertEqual(self.guard.state['overnight_exposure']['status'], 'present')

    def test_verified_new_baseline_keeps_daily_monitoring(self):
        self.guard.baseline_file.write_text(json.dumps(baseline(self.profile, now=self.after)))
        self.rollover()
        self.assertTrue(self.guard.state['daily_monitoring_available'])
        self.assertEqual(self.guard.state['monitoring_status'], 'daily_and_overall')
        self.assertEqual(self.guard.state['operating_daily_floor'], 4820.)
        self.direct.snapshot.assert_not_called()

    def test_bracket_retains_real_receipts_without_midnight_certification(self):
        self.rollover()
        estimate = self.guard.state['baseline_estimate']
        self.assertEqual([s['received_at'] for s in estimate['samples']],
                         [self.before.isoformat(), self.after.isoformat()])
        self.assertFalse(estimate['confirmed'])
        self.assertIsNone(self.guard.state['baseline'])

    def test_flat_observation_does_not_certify_daily_or_unlock(self):
        self.direct.snapshot.return_value = {'positions': [], 'orders': []}
        self.rollover()
        self.assertEqual(self.guard.state['overnight_exposure']['status'], 'flat_observed')
        self.assertTrue(self.guard.state['data_fault'])
        self.assertIsNone(self.guard.state['baseline'])

    def test_exposure_failure_is_unknown_not_flat(self):
        self.direct.snapshot.side_effect = TimeoutError('offline')
        self.rollover()
        self.assertEqual(self.guard.state['overnight_exposure']['status'], 'unknown')
        self.assertEqual(self.guard.state['operating_overall_floor'], 4740.)
        self.emergency.step.assert_not_called()

    def test_overall_emergency_does_not_wait_for_exposure_query(self):
        self.direct.equity.return_value = 4740.
        self.guard._check_drawdown(self.after)
        self.assertTrue(self.guard.state['nuclear_triggered'])
        self.direct.snapshot.assert_not_called()
        self.emergency.step.assert_called_once()
        self.assertFalse(self.guard.state['liquidation_confirmed'])

    def test_valid_restart_keeps_daily_fault(self):
        self.rollover()
        restarted = RiskGuard(self.api, self.path, profile=self.profile, emergency=self.emergency)
        self.assertTrue(restarted.state['data_fault'])
        self.assertTrue(restarted.state['overnight_policy_required'])
        self.assertFalse(prop_capital_available(self.root, self.after, self.config))

    def test_corrupt_state_preserved_and_daily_gap_persisted_in_journal(self):
        self.path.write_text('{broken')
        restarted = RiskGuard(self.api, self.path, profile=self.profile, emergency=self.emergency)
        restarted._check_drawdown(self.after)
        self.assertEqual(self.path.read_text(), '{broken')
        self.assertEqual(restarted.state['overnight_exposure']['status'], 'present')
        events = [r[0] for r in self.journal.db.execute('SELECT reason FROM fault_events')]
        self.assertTrue(any('Daily floor unverifiable' in e for e in events))
        self.emergency.step.assert_not_called()

    def test_cycle_start_before_midnight_receipt_after_uses_new_day(self):
        self.guard._check_drawdown(self.before)
        with patch('prop_risk_guard.datetime') as clock:
            clock.now.side_effect = [self.before, self.after]
            self.guard._check_drawdown()
        obs = self.guard.state['equity_observation']
        self.assertEqual(obs['cycle_started_at'], self.before.isoformat())
        self.assertEqual(obs['received_at'], self.after.isoformat())
        self.assertEqual(self.guard.state['current_trading_day'], self.after.date().isoformat())
        self.assertIsNone(self.guard.state['baseline'])

    def test_response_from_previous_day_cannot_monitor_new_daily(self):
        self.guard.baseline_file.write_text(json.dumps(baseline(self.profile, now=self.after)))
        self.direct.equity_observation = dict(equity=5000., response_time_ms=self.before.timestamp()*1000,
                                             source='bybit_v5_wallet_response')
        self.guard._check_drawdown(self.after)
        self.assertFalse(self.guard.state['daily_monitoring_available'])
        self.assertEqual(self.guard.state['equity_observation']['response_at'], self.before.isoformat())
        self.emergency.step.assert_not_called()

    def test_return_of_baseline_does_not_clear_persistent_fault(self):
        self.rollover()
        self.guard.baseline_file.write_text(json.dumps(baseline(self.profile, now=self.after)))
        self.guard._check_drawdown(self.after)
        self.assertTrue(self.guard.state['daily_monitoring_available'])
        self.assertTrue(self.guard.state['data_fault'])
        self.control.start_trading.assert_not_called()


if __name__ == '__main__':
    unittest.main()
