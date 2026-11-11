"""Negative acquisition proofs: current observations cannot certify midnight history."""
import unittest
from unittest.mock import Mock, patch

import test_prop_rollover as fixtures
from freqtrade.supervisor_bybit import SupervisorBybit, UnverifiedAccount
from freqtrade.prop_profile import validate_baseline
from freqtrade.prop_capital import prop_capital_available
from prop_risk_guard import RiskGuard
from test_prop_profile import baseline


class BaselineAcquisitionTests(unittest.TestCase):
    setUp = fixtures.RolloverTests.setUp
    rollover = fixtures.RolloverTests.rollover

    def adapter(self, *, equity=5080., wallet=5000., response_at=None):
        api = Mock()
        adapter = SupervisorBybit(api, self.profile)
        adapter.account_type = 'UNIFIED'
        stamp = (response_at or self.after).timestamp() * 1000
        api.privateGetV5AccountWalletBalance.return_value = {
            'retCode': 0, 'time': stamp, 'result': {'list': [{
                'accountType': 'UNIFIED', 'coin': [{'coin': 'USDT', 'equity': str(equity),
                    'walletBalance': str(wallet), 'borrowAmount': '0', 'unrealisedPnl': str(equity-wallet)}]
            }]}}
        return adapter, stamp

    def test_real_adapter_preserves_equity_including_floating_pnl_once(self):
        adapter, stamp = self.adapter()
        with patch('freqtrade.supervisor_bybit.time.time', return_value=stamp / 1000):
            self.assertEqual(adapter.equity(), 5080.)
        self.assertEqual(adapter.equity_observation['response_time_ms'], stamp)
        self.assertNotIn('confirmed', adapter.equity_observation)

    def test_response_exactly_midnight_is_not_a_certified_snapshot(self):
        midnight = self.after.replace(hour=0, minute=0, second=0, microsecond=0)
        adapter, stamp = self.adapter(equity=5000., response_at=midnight)
        with patch('freqtrade.supervisor_bybit.time.time', return_value=stamp / 1000):
            value = adapter.equity()
        self.direct.equity.return_value = value
        self.direct.equity_observation = adapter.equity_observation
        self.guard._check_drawdown(self.after)
        self.assertIsNone(self.guard.state['baseline'])
        self.assertFalse(self.guard.baseline_file.exists())
        self.assertFalse(self.guard.state['daily_monitoring_available'])
        self.emergency.step.assert_not_called()

    def test_flat_current_equity_does_not_exclude_capital_transfers(self):
        # Both histories produce the same observable current flat wallet.
        # Neither history is supplied by the current adapter's endpoint set.
        for historical_midnight, transfer in ((5000., 0.), (4900., 100.)):
            with self.subTest(transfer=transfer):
                self.assertEqual(historical_midnight + transfer, 5000.)
                adapter, _ = self.adapter(equity=5000.)
                with self.assertRaises(UnverifiedAccount):
                    adapter.certify_flat_midnight_baseline(self.after)
                adapter.api.privateGetV5AccountWalletBalance.assert_not_called()

    def test_incomplete_order_history_cannot_reconstruct_wallet_equity(self):
        adapter, _ = self.adapter()
        adapter.lookup = Mock(return_value={'status': 'closed', 'filled': 1.})
        with self.assertRaises(UnverifiedAccount):
            adapter.certify_flat_midnight_baseline(self.after)
        adapter.lookup.assert_not_called()

    def test_reviewed_external_contract_is_accepted_but_scalar_is_not(self):
        # Structural validation only: this fixture is not evidence obtained from Bybit.
        record = baseline(self.profile, now=self.after)
        self.assertEqual(validate_baseline(record, self.profile, self.after), record)
        for changes in ({'evidence': None}, {'confirmed': False}, {'status': 'estimated'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_baseline({**record, **changes}, self.profile, self.after)
        other = {**record, 'profile': {**self.profile, 'challenge_id': 'other'}}
        with self.assertRaises(ValueError):
            validate_baseline(other, self.profile, self.after)

    def test_missing_state_and_journal_never_bootstrap_a_baseline(self):
        missing = self.root / 'uncommissioned' / 'risk_manager_prop_state.json'
        guard = RiskGuard(self.api, missing, profile=self.profile, emergency=self.emergency)
        guard._check_drawdown(self.after)
        self.assertTrue(guard.state['data_fault'])
        self.assertFalse(guard.state['daily_monitoring_available'])
        self.assertFalse(missing.exists())
        self.assertFalse(guard.baseline_file.exists())
        self.assertFalse(prop_capital_available(missing.parent, self.after, self.config))
        self.emergency.step.assert_not_called()

    def test_failed_atomic_state_replace_preserves_previous_file_and_blocks(self):
        original = self.path.read_bytes()
        with patch('pathlib.Path.replace', side_effect=OSError('offline storage unavailable')):
            self.guard._check_drawdown(self.after)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(self.guard.state['data_fault'])
        self.assertIsNotNone(self.guard.state_error)
        self.assertFalse(prop_capital_available(self.root, self.after, self.config))
        restarted = RiskGuard(self.api, self.path, profile=self.profile, emergency=self.emergency)
        restarted._check_drawdown(self.after)
        self.assertFalse(restarted.state['daily_monitoring_available'])
        self.emergency.step.assert_not_called()

    def test_exchange_unavailable_never_substitutes_initial_capital(self):
        self.direct.equity.side_effect = UnverifiedAccount('offline unavailable')
        self.guard._check_drawdown(self.after)
        self.assertEqual(self.guard.state['monitoring_status'], 'unavailable')
        self.assertFalse(self.guard.state['data_valid'])
        self.assertFalse(self.guard.state['daily_monitoring_available'])
        self.assertFalse(self.guard.baseline_file.exists())
        self.emergency.step.assert_not_called()

    def test_pre_post_values_do_not_establish_numeric_uncertainty_interval(self):
        self.guard._check_drawdown(self.before)
        self.direct.equity.return_value = 4990.
        self.guard._check_drawdown(self.after)
        estimate = self.guard.state['baseline_estimate']
        self.assertEqual([s['equity'] for s in estimate['samples']], [5000., 4990.])
        # An unobserved equity of 5100 at midnight is not excluded by these endpoints.
        self.assertNotIn('equity', estimate)
        self.assertNotIn('lower_bound', estimate)
        self.assertNotIn('upper_bound', estimate)
        self.assertFalse(estimate['confirmed'])
        self.assertFalse(prop_capital_available(self.root, self.after, self.config))


if __name__ == '__main__':
    unittest.main()
