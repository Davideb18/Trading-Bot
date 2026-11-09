"""Patch 1 offline regression proofs, no exchange/HTTP connection permitted."""
import copy
import io
import json
import logging
import socket
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
from test_prop_profile import baseline, configured_account
from freqtrade.prop_profile import profile_from_config, validate_baseline
from freqtrade.prop_capital import prop_capital_available
from freqtrade.prop_risk_budget import require_risk_budget
from freqtrade.order_journal import JournalBlocked, OrderJournal
from prop_risk_guard import RiskGuard, reconciled_state


class BaselineSafetyTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(socket.socket, 'connect', side_effect=AssertionError('Network forbidden')).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = configured_account()
        self.config['user_data_dir'] = self.root
        self.profile = profile_from_config(self.config)
        self.now = datetime.now(UTC)
        self.path = self.root / 'risk_manager_prop_state.json'
        self.verified = baseline(self.profile, now=self.now)
        self.path.write_text(json.dumps(reconciled_state(self.profile, self.verified, self.now, halted=False)))
        self.api = Mock()
        self.api.get_prop_profile.return_value = self.profile
        self.api.get_equity.return_value = 5000
        self.api.get_prop_control.return_value = dict(verified=True, flat=True, pending_entries=False, ack=None)
        self.emergency = Mock()
        self.emergency.step.return_value = False

    def guard(self):
        return RiskGuard(self.api, self.path, profile=self.profile, emergency=self.emergency)

    def test_verified_external_evidence_accepted_but_manual_scalar_rejected(self):
        validate_baseline(self.verified, self.profile, self.now)
        for changes in ({'evidence': None}, {'status': 'estimated'}, {'confirmed': False},
                        {'source': 'bybit_v5_flat_account_verified'},
                        {'source': 'bybit_v5_midnight_conservative_bracket'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_baseline({**self.verified, **changes}, self.profile, self.now)

    def test_historical_evidence_must_match_account_value_and_instant(self):
        for changes in ({'account_id': 'other'}, {'equity': 4999}, {'reviewed': False},
                        {'reference': ''}, {'observed_at': self.now.isoformat()}):
            candidate = copy.deepcopy(self.verified)
            candidate['evidence'].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_baseline(candidate, self.profile, self.now)

    def test_legacy_bracket_restart_locked_without_rewriting_original(self):
        state = json.loads(self.path.read_text())
        state['baseline']['source'] = 'bybit_v5_midnight_conservative_bracket'
        self.path.write_text(json.dumps(state))
        original = self.path.read_bytes()
        guard = self.guard()
        guard._check_drawdown(self.now)
        self.assertIsNotNone(guard.state_error)
        self.assertEqual(guard.state['monitoring_status'], 'overall_only')
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(prop_capital_available(self.root, self.now, self.config))
        self.emergency.step.assert_not_called()

    def test_missing_corrupt_and_estimated_baselines_still_check_overall(self):
        tomorrow = self.now + timedelta(days=1)
        for content in (None, '{broken', json.dumps({**self.verified, 'status': 'estimated'})):
            with self.subTest(content=content):
                guard = self.guard()
                if content is not None:
                    guard.baseline_file.write_text(content)
                elif guard.baseline_file.exists():
                    guard.baseline_file.unlink()
                self.api.get_equity.return_value = 5000
                guard._check_drawdown(tomorrow)
                self.assertIsNone(guard.state['baseline'])
                self.assertFalse(guard.state['data_valid'])
                self.assertTrue(guard.state['data_fault'])
                self.assertIsNone(guard.state['operating_daily_floor'])
                self.api.get_equity.return_value = 4740
                guard._check_drawdown(tomorrow + timedelta(seconds=1))
                self.assertTrue(guard.nuclear_triggered)
                # Emergency adapter remains the authority; False is not flat.
                self.assertFalse(guard.state['liquidation_confirmed'])
                self.path.write_text(json.dumps(reconciled_state(self.profile, self.verified, self.now, halted=False)))

    def test_new_verified_baseline_does_not_clear_persistent_fault(self):
        guard = self.guard()
        tomorrow = self.now + timedelta(days=1)
        guard._check_drawdown(tomorrow)
        guard.baseline_file.write_text(json.dumps(baseline(self.profile, now=tomorrow)))
        guard._check_drawdown(tomorrow)
        self.assertEqual(guard.state['baseline_status'], 'verified')
        self.assertTrue(guard.state['data_fault'])
        self.api.start_trading.assert_not_called()

    def test_state_fault_reads_direct_before_slow_or_failing_http(self):
        for failure in (TimeoutError, ConnectionError):
            self.path.write_text('{broken')
            events = []
            self.api.get_prop_profile.side_effect = lambda: events.append('identity') or self.profile
            self.api.get_equity.side_effect = lambda: events.append('equity') or 5000
            def http(*args):
                events.append('http')
                raise failure('offline simulated timeout')
            self.api.report_fault.side_effect = http
            guard = self.guard()
            with self.assertRaises(failure):
                guard._check_drawdown(self.now)
            self.assertEqual(events[:3], ['identity', 'equity', 'http'])
            self.assertEqual(guard.state['monitoring_status'], 'overall_only')
            self.emergency.step.assert_not_called()

    def test_state_fault_breach_reaches_existing_emergency_without_http(self):
        self.path.write_text('{broken')
        self.api.get_equity.return_value = 4740
        self.api.report_fault.side_effect = AssertionError('HTTP must not precede emergency')
        guard = self.guard()
        guard._check_drawdown(self.now)
        self.emergency.step.assert_called_once()
        self.assertTrue(guard.nuclear_triggered)
        self.assertFalse(guard.state['liquidation_confirmed'])
        self.api.report_fault.assert_not_called()

    def test_direct_failure_explicitly_reports_monitor_unavailable(self):
        self.path.write_text('{broken')
        self.api.get_equity.return_value = None
        guard = self.guard()
        with self.assertLogs('prop_risk_guard', level='ERROR') as captured:
            guard._check_drawdown(self.now)
        self.assertEqual(guard.state['monitoring_status'], 'unavailable')
        self.assertIn('monitoring unavailable', ' '.join(captured.output))
        self.emergency.step.assert_not_called()

    def test_entry_dca_replacement_cannot_use_estimated_baseline(self):
        identity = {k: self.profile[k] for k in ('profile_id', 'account_id', 'challenge_id',
                    'phase_id', 'exchange', 'environment', 'currency')}
        journal = OrderJournal(self.root / 'order_journal', identity)
        self.addCleanup(journal.close)
        state = json.loads(self.path.read_text())
        state.update(data_valid=True, data_fault=False, wake_up_lock=False, remote_identity_verified=True)
        state['baseline']['status'] = 'estimated'
        self.path.write_text(json.dumps(state))
        bot = SimpleNamespace(config=self.config, order_journal=journal)
        for initial in (True, False):
            with self.assertRaises(JournalBlocked):
                require_risk_budget(bot, dict(pair='BTC/USDT:USDT', side='buy', amount=1,
                    rate=100, reduceOnly=False, initial_order=initial))
        self.assertFalse(prop_capital_available(self.root, self.now, self.config))
        self.assertEqual(journal.intents(), [])
        self.assertTrue(all(i['state'] == 'risk_denied' for i in journal.intents(active=False)))

    def test_logging_falls_back_to_stderr_without_sensitive_exception(self):
        from llm_risk_manager_prop import configure_logging
        stream = io.StringIO()
        with patch('llm_risk_manager_prop.LOG_DIR') as directory, \
                patch('llm_risk_manager_prop.logging.FileHandler', side_effect=PermissionError('sensitive-path')), \
                patch('llm_risk_manager_prop.logging.basicConfig') as setup, patch.object(sys, 'stderr', stream):
            with self.assertLogs('llm_risk_manager_prop', level='ERROR') as captured:
                self.assertFalse(configure_logging())
            handlers = setup.call_args.kwargs['handlers']
            self.assertEqual(len(handlers), 1)
            self.assertIs(handlers[0].stream, stream)
            self.assertNotIn('sensitive-path', ' '.join(captured.output))
            handlers[0].close()


if __name__ == '__main__':
    unittest.main()
