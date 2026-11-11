"""Offline approval binding regressions; all reviews and exchange results are fixtures."""
import copy
import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import test_prop_risk_budget as fixtures
from test_prop_risk_budget import approved_budget_policy, seal_test_policy
from test_order_journal import REQUEST, snapshot, order, local
from freqtrade.order_journal import JournalBlocked, OrderJournal
from freqtrade.prop_risk_approval import policy_identity
from freqtrade.prop_risk_budget import require_risk_budget


class ApprovalTests(unittest.TestCase):
    save = fixtures.RiskBudgetTests.save
    deny = fixtures.RiskBudgetTests.deny

    def setUp(self):
        fixtures.RiskBudgetTests.setUp(self)
        self.policy = approved_budget_policy(self.profile)
        self.config['prop_risk_budget'] = self.policy
        self.state['remote_identity_verified'] = True
        self.save()

    def decision(self):
        return require_risk_budget(self.bot, {**REQUEST, 'amount': 1.}, 7)

    def test_canonical_order_independent_and_detached(self):
        original = policy_identity(self.profile, self.policy, self.config, REQUEST)
        reverse = dict(reversed(list(self.policy.items())))
        self.assertEqual(original, policy_identity(self.profile, reverse, self.config, REQUEST))
        self.policy['roundtrip_fee_bps'] = 13.
        self.assertNotEqual(original[1], policy_identity(self.profile, self.policy, self.config, REQUEST)[1])
        self.assertEqual(json.loads(original[0])['policy']['roundtrip_fee_bps'], 12.)

    def test_missing_wrong_fingerprint_and_schema(self):
        original = copy.deepcopy(self.policy['approval'])
        for change in ({'policy_fingerprint': None}, {'policy_fingerprint': '0' * 64},
                       {'schema_version': 2}, {'schema_version': True}):
            with self.subTest(change=change):
                self.policy['approval'] = {**original, **change}
                self.assertFalse(self.deny()['authorized'])
        del self.policy['approval']
        self.assertFalse(self.deny()['authorized'])

    def test_each_parameter_change_invalidates_previous_approval(self):
        for key in ('roundtrip_fee_bps', 'funding_bound_bps', 'execution_slippage_bps',
                    'latency_gap_stress_bps', 'min_headroom_buffer_usdt',
                    'max_slot_notional_usdt', 'max_aggregate_notional_usdt'):
            with self.subTest(key=key):
                old = self.policy[key]
                self.assertTrue(self.decision()['authorized'])
                self.policy[key] = old + 1
                self.assertEqual(self.deny()['reason'], 'unapproved_risk_budget_parameters')
                self.policy[key] = old

    def test_identity_phase_environment_currency_capital_bound(self):
        fingerprint = policy_identity(self.profile, self.policy, self.config, REQUEST)[1]
        for key in ('account_id', 'challenge_id', 'phase_id', 'environment', 'exchange',
                    'currency', 'initial_balance', 'phase_start_utc'):
            with self.subTest(key=key):
                changed = {**self.profile, key: 'different'}
                self.assertNotEqual(fingerprint, policy_identity(changed, self.policy, self.config, REQUEST)[1])
        for key in ('account_id', 'challenge_id', 'phase_id'):
            original = self.config['prop_guard'][key]
            self.config['prop_guard'][key] = 'different'
            self.assertFalse(self.deny()['authorized'])
            self.config['prop_guard'][key] = original
        self.config['exchange']['demo_trading'] = False
        self.assertFalse(self.deny()['authorized'])

    def test_operating_modes_and_leverage_change_invalidates(self):
        original = copy.deepcopy(self.config)
        for key, value in (('max_open_trades', 3), ('margin_mode', 'cross'),
                           ('order_types', {'entry': 'market'}), ('dry_run', True),
                           ('nfi_parameters', {'futures_mode_leverage_grind_mode': 2.})):
            with self.subTest(key=key):
                self.config[key] = value
                self.assertFalse(self.deny()['authorized'])
                self.config[key] = original[key]
        self.assertFalse(self.deny(leverage=None)['authorized'])
        self.assertFalse(self.deny(ordertype='market')['authorized'])
        self.assertFalse(self.deny(time_in_force='IOC')['authorized'])

    def test_unsupported_model_not_rescued_by_fresh_fingerprint(self):
        self.policy['model'] = 'unsupported-v3'
        seal_test_policy(self.profile, self.policy, self.config)
        self.assertEqual(self.deny()['reason'], 'uncertified_aggregate_loss_and_cost_model')

    def test_hash_without_identifiable_review_is_insufficient(self):
        original = copy.deepcopy(self.policy['approval'])
        for changes in ({'approved_by': ''}, {'reference': ''}, {'reviewed': False},
                        {'reviewed_at': '2026-11-11T00:00:00'},
                        {'reviewed_at': (datetime.now(UTC) + timedelta(days=1)).isoformat()},
                        {'reviewed_at': None}):
            with self.subTest(changes=changes):
                self.policy['approval'] = {**original, **changes}
                self.assertFalse(self.deny()['authorized'])

    def test_restart_with_obsolete_approval(self):
        self.assertTrue(self.decision()['authorized'])
        self.policy['execution_slippage_bps'] += 1
        self.j.close()
        self.j = OrderJournal(Path(self.tmp.name) / 'order_journal', self.identity)
        self.bot.order_journal = self.j
        self.j.reconcile(snapshot(), {}, {})
        self.assertEqual(self.deny()['reason'], 'unapproved_risk_budget_parameters')

    def test_pending_provenance_costs_and_reservation_survive_policy_change(self):
        decision = self.decision()
        cid = self.j.prepare({**REQUEST, 'amount': 1.}, 7, reservation=decision)
        before = self.j.db.execute('SELECT detail FROM risk_reservations WHERE client_id=?', (cid,)).fetchone()[0]
        stored = json.loads(before)
        self.assertEqual(stored['loss_bound'], 3.)
        self.assertAlmostEqual(stored['loss_bound'], stored['future_cost_bound'] + stored['future_loss_bound'])
        self.assertEqual(stored['policy_fingerprint'], self.policy['approval']['policy_fingerprint'])
        self.assertEqual(stored['approval_evidence']['approved_by'], 'offline-auditor')
        # Simulate an open exchange order: only its remainder enters inventory.
        o = order(cid)
        o.update(amount=1., remaining=1.)
        self.j.reconcile(snapshot(o), local(o), {})
        self.policy['funding_bound_bps'] += 1
        self.assertFalse(self.deny(initial_order=False)['authorized'])
        row = self.j.db.execute('SELECT state, detail FROM risk_reservations WHERE client_id=?', (cid,)).fetchone()
        self.assertEqual(tuple(row), ('held', before))
        self.assertEqual(self.j.db.execute("SELECT count(*) FROM risk_reservations WHERE state='held'").fetchone()[0], 1)
        terminal = {**o, 'status': 'canceled', 'remaining': 0.}
        self.j.reconcile(snapshot(terminal), {}, {})
        row = self.j.db.execute('SELECT state, detail FROM risk_reservations WHERE client_id=?', (cid,)).fetchone()
        self.assertEqual(tuple(row), ('reconciled', before))

    def test_legacy_pending_intent_not_retroactively_approved(self):
        cid = self.j.prepare(REQUEST, 7, reservation={'authorized': True, 'model': 'previous', 'loss_bound': 7.})
        before = self.j.db.execute('SELECT detail FROM risk_reservations WHERE client_id=?', (cid,)).fetchone()[0]
        self.j.close()
        self.j = OrderJournal(Path(self.tmp.name) / 'order_journal', self.identity)
        self.bot.order_journal = self.j
        self.j.reconcile(snapshot(), {}, {})
        self.assertFalse(self.deny()['authorized'])
        self.assertEqual(self.j.db.execute('SELECT detail FROM risk_reservations WHERE client_id=?', (cid,)).fetchone()[0], before)
        self.assertNotIn('policy_fingerprint', json.loads(before))

    def test_dca_replacement_denied_before_dispatch(self):
        from freqtrade.journal_execution import journal_create_order
        from types import SimpleNamespace
        self.policy['min_headroom_buffer_usdt'] += 1
        self.bot.exchange = SimpleNamespace(fetch_journal_snapshot=Mock(return_value=snapshot()), create_order=Mock())
        self.bot.has_unresolved_orders = lambda: False
        model = SimpleNamespace(get_trades_proxy=lambda: [], commit=Mock())
        for tag in ('dca', 'replacement'):
            with self.subTest(tag=tag), self.assertRaises(JournalBlocked):
                journal_create_order(self.bot, None, model, **{**REQUEST, 'initial_order': False})
        self.bot.exchange.create_order.assert_not_called()
        self.assertEqual(self.j.db.execute("SELECT count(*) FROM risk_reservations WHERE state='held'").fetchone()[0], 0)

    def test_reduce_only_and_emergency_with_invalid_approval(self):
        self.policy['approval'] = {}
        require_risk_budget(self.bot, {**REQUEST, 'reduceOnly': True})
        fixtures.RiskBudgetTests.test_emergency_authority_works_after_zero_budget_denial(self)


if __name__ == '__main__':
    unittest.main()
