"""Offline P0.6 proofs. Positive headroom is not certified order risk."""
import json
import socket
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from snapshot_fixtures import reads

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from freqtrade.order_journal import JournalBlocked, OrderJournal
from freqtrade.prop_profile import profile_from_config
from freqtrade.prop_risk_budget import budget, inventory, require_risk_budget
from freqtrade.prop_risk_approval import policy_identity, APPROVAL_SCHEMA
from freqtrade.journal_execution import journal_create_order
from test_prop_profile import configured_account, baseline
from test_order_journal import REQUEST, PAIR, snapshot, order, local


def approved_budget_policy(profile):
    policy = {
        "model": "hyrotrader_one_step_validated_v2",
        "status": "approved",
        "approval": {
            "kind": "risk_budget_approval",
            "reviewed": True,
            "account_id": profile["account_id"],
            "challenge_id": profile["challenge_id"],
            "reference": "offline-risk-policy-cert",
            "approved_by": "offline-auditor",
        },
        "max_slot_notional_usdt": 750.0,
        "max_aggregate_notional_usdt": 1200.0,
        "roundtrip_fee_bps": 12.0,
        "funding_bound_bps": 10.0,
        "execution_slippage_bps": 28.0,
        "latency_gap_stress_bps": 250.0,
        "min_headroom_buffer_usdt": 30.0,
    }
    # Synthetic review fixture only. Never imported by commissioning or runtime.
    seal_test_policy(profile, policy, configured_account())
    return policy


def seal_test_policy(profile, policy, config, request=REQUEST):
    policy['approval'].update(
        schema_version=APPROVAL_SCHEMA,
        reviewed_at=datetime.now(UTC).isoformat(),
        policy_fingerprint=policy_identity(profile, policy, config, request)[1],
    )


class RiskBudgetTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(socket.socket, 'connect', side_effect=AssertionError('Offline only')).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = configured_account()
        self.config['user_data_dir'] = self.tmp.name
        self.profile = profile_from_config(self.config)
        self.identity = {k: self.profile[k] for k in (
            'profile_id', 'account_id', 'challenge_id', 'phase_id', 'exchange', 'environment', 'currency')}
        self.j = OrderJournal(Path(self.tmp.name) / 'order_journal', self.identity)
        self.addCleanup(lambda: self.j.close())
        self.j.reconcile(snapshot(), {}, {})
        self.bot = SimpleNamespace(config=self.config, order_journal=self.j)
        self.state = dict(profile=self.profile, risk_schema_version=2, control_schema_version=1,
                          last_updated=datetime.now(UTC).isoformat(),
                          baseline=baseline(self.profile, now=datetime.now(UTC)),
                          data_fault=False, nuclear_triggered=False, macro_paused=False,
                          data_valid=True, wake_up_lock=False, last_equity=5000)
        self.state['risk_component_reads'] = reads('identity', 'account_mode', 'equity')
        self.save()

    def save(self):
        (Path(self.tmp.name) / 'risk_manager_prop_state.json').write_text(json.dumps(self.state))

    def deny(self, **changes):
        with self.assertRaises(JournalBlocked):
            require_risk_budget(self.bot, {**REQUEST, **changes}, 7)
        with self.j.mutex:
            row = self.j.db.execute('SELECT * FROM risk_reservations ORDER BY rowid DESC').fetchone()
        self.assertEqual(row['state'], 'denied')
        return json.loads(row['detail'])

    def test_headroom_positive_low_and_exhausted(self):
        for equity, expected in [(5000, 180), (4821, 1), (4820, 0), (4800, 0)]:
            with self.subTest(equity=equity):
                self.assertEqual(budget(self.profile, 5000, equity)['headroom'], expected)
                self.state['last_equity'] = equity
                self.save()
                result = self.deny()
                self.assertEqual(result['headroom'], expected)
                self.assertFalse(result['authorized'])

    def test_static_overall_floor_dominates(self):
        self.assertEqual(budget(self.profile, 4800, 4800),
                         dict(operating_floor=4740, headroom=60))

    def test_concurrent_entry_dca_replacement_unchanged_never_dispatchable(self):
        def attempt(i):
            return self.deny(amount=i + 0.125, initial_order=(i == 0))
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(attempt, range(3)))
        intents = self.j.intents(active=False)
        self.assertEqual(sorted(json.loads(i['request'])['amount'] for i in intents), [.125, 1.125, 2.125])
        for i in intents:
            send = Mock()
            with self.assertRaises(JournalBlocked):
                self.j.dispatch(i['client_id'], send)
            send.assert_not_called()

    def test_partial_fill_counted_in_position_and_only_residual_pending(self):
        cid = self.j.prepare(REQUEST, 7)
        o = order(cid, filled=.5)
        positions = [dict(symbol=PAIR, side='long', amount=.5)]
        self.j.reconcile(snapshot(o, positions), local(o), {(PAIR, 'long'): .5})
        result = inventory(self.j)
        self.assertEqual(result['positions'][0]['amount'], .5)
        self.assertEqual(result['pending_entries'][0]['amount'], 1.5)
        self.assertEqual(result['unobserved_reservations'], [])
        self.assertIsNone(result['future_cost_bound'])
        self.assertEqual(self.deny()['headroom'], 180)  # No second floating P&L deduction.
        self.assertEqual(self.j.db.execute("SELECT state FROM risk_reservations WHERE client_id=?", (cid,)).fetchone()[0], 'held')

    def test_release_only_after_complete_terminal_reconciliation(self):
        cid = self.j.prepare(REQUEST, 7)
        o = order(cid, status='closed', filled=2)
        self.j.reconcile(snapshot(o), local(o), {(PAIR, 'long'): 2})
        self.assertEqual(self.j.db.execute('SELECT state FROM risk_reservations').fetchone()[0], 'held')
        self.j.reconcile(snapshot(o, [dict(symbol=PAIR, side='long', amount=2)]), local(o), {(PAIR, 'long'): 2})
        self.assertEqual(self.j.db.execute('SELECT state FROM risk_reservations').fetchone()[0], 'reconciled')
        self.assertEqual(len(inventory(self.j)['positions']), 1)

    def test_timeout_crash_restart_preserve_reservation(self):
        for error in (TimeoutError, KeyboardInterrupt):
            with self.subTest(error=error):
                self.j.ready = True  # Isolate two legacy pending intents.
                cid = self.j.prepare(REQUEST)
                with self.assertRaises(error):
                    self.j.dispatch(cid, Mock(side_effect=error))
        self.j.close()
        self.j = OrderJournal(Path(self.tmp.name) / 'order_journal', self.identity)
        self.assertFalse(self.j.ready)
        self.assertEqual([r[0] for r in self.j.db.execute('SELECT state FROM risk_reservations')], ['held', 'held'])
        self.j.reconcile(snapshot(), {}, {})
        self.assertEqual([r[0] for r in self.j.db.execute('SELECT state FROM risk_reservations')], ['held', 'held'])

    def test_second_instance_excluded(self):
        with self.assertRaises(JournalBlocked):
            OrderJournal(Path(self.tmp.name) / 'order_journal', self.identity)

    def test_legacy_intent_backfilled_without_inventing_budget(self):
        cid = self.j.prepare(REQUEST)
        with self.j.db:
            self.j.db.execute('DELETE FROM risk_reservations')
        self.j.close()
        self.j = OrderJournal(Path(self.tmp.name) / 'order_journal', self.identity)
        row = self.j.db.execute('SELECT * FROM risk_reservations').fetchone()
        self.assertEqual(row['client_id'], cid)
        self.assertEqual(row['state'], 'held')
        self.assertIsNone(json.loads(row['detail'])['loss_bound'])

    def test_unobserved_reservation_is_not_zero(self):
        cid = self.j.prepare(REQUEST)
        # Last snapshot alone is insufficient to authorize after prepare.
        self.assertEqual(inventory(self.j)['unobserved_reservations'], [cid])
        self.assertIn('unverified_risk_context', self.deny()['reason'])

    def test_cancelled_pending_released_before_replacement_is_denied(self):
        cid = self.j.prepare(REQUEST)
        o = order(cid, status='canceled')
        self.assertEqual(self.j.reconcile(snapshot(o), {}, {}), [])
        self.assertEqual(self.j.db.execute('SELECT state FROM risk_reservations').fetchone()[0], 'reconciled')
        result = self.deny(initial_order=False)
        self.assertEqual(result['pending_entries'], [])
        self.assertEqual(result['unobserved_reservations'], [])
        self.assertFalse(result['authorized'])

    def test_stale_equity_fault_missing_baseline_identity(self):
        original = dict(self.state)
        for change in (
            {'last_updated': (datetime.now(UTC) - timedelta(seconds=30)).isoformat()},
            {'data_fault': True}, {'baseline': {}}, {'profile': {}}, {'last_equity': float('nan')},
        ):
            with self.subTest(change=change):
                self.state = {**original, **change}
                self.save()
                self.assertIn('unverified_risk_context', self.deny()['reason'])

    def test_stale_reconciliation(self):
        self.j.last_reconciliation['checked_ms'] = int(time.time() * 1000) - 16000
        self.assertIn('Stale', self.deny()['reason'])

    def test_atomic_rollback_on_reservation_failure(self):
        self.j.db.execute("CREATE TRIGGER reject_reservation BEFORE INSERT ON risk_reservations BEGIN SELECT RAISE(ABORT, 'test'); END")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            require_risk_budget(self.bot, REQUEST)
        self.assertEqual(self.j.intents(active=False), [])

    def test_reduction_bypasses_budget_even_without_state(self):
        self.bot.config = {}
        require_risk_budget(self.bot, {**REQUEST, 'reduceOnly': True})
        self.assertEqual(self.j.intents(active=False), [])

    def test_emergency_authority_works_after_zero_budget_denial(self):
        self.state['last_equity'] = 4820
        self.save()
        self.assertEqual(self.deny()['headroom'], 0)
        with self.j.db:
            self.j.db.execute('INSERT INTO emergency VALUES (1,?)', (json.dumps(dict(
                owner='offline-supervisor', handoff=True, phase='liquidating', halt_id='test')),))
        request = {**REQUEST, 'reduceOnly': True, 'side': 'sell', 'ordertype': 'market'}
        cid = self.j.prepare_emergency(request, 'offline-supervisor')
        send = Mock(return_value={'id': 'offline-exit', 'clientOrderId': cid})
        self.j.dispatch(cid, send)
        send.assert_called_once_with(**request, client_order_id=cid)
        self.assertIsNone(self.j.db.execute('SELECT * FROM risk_reservations WHERE client_id=?', (cid,)).fetchone())

    def test_journal_identity_mismatch_denied(self):
        with self.j.db:
            self.j.db.execute('UPDATE binding SET value=?',
                              (json.dumps({**self.identity, 'account_id': 'another-account'}),))
        self.assertIn('identity mismatch', self.deny()['reason'])

    def test_real_integration_blocks_before_exchange(self):
        self.bot.exchange = SimpleNamespace(fetch_journal_snapshot=Mock(return_value=snapshot()), create_order=Mock())
        self.bot.has_unresolved_orders = lambda: False
        model = SimpleNamespace(get_trades_proxy=lambda: [], commit=Mock())
        with self.assertRaisesRegex(JournalBlocked, 'P0.6'):
            journal_create_order(self.bot, None, model, **REQUEST)
        self.bot.exchange.create_order.assert_not_called()

    def test_validated_v2_budget_authorizes_within_slot_aggregate_and_stress_bounds(self):
        self.bot.config['prop_risk_budget'] = approved_budget_policy(self.profile)
        self.state['remote_identity_verified'] = True
        self.save()
        decision = require_risk_budget(self.bot, {**REQUEST, 'amount': 1.0, 'rate': 100.0}, 7)
        self.assertTrue(decision['authorized'])
        self.assertEqual(decision['model'], 'hyrotrader_one_step_validated_v2')
        self.assertAlmostEqual(decision['future_cost_bound'], 0.22)
        self.assertAlmostEqual(decision['future_loss_bound'], 2.78)
        self.assertAlmostEqual(decision['loss_bound'], 3.0)
        self.assertAlmostEqual(decision['required_headroom'], 33.0)
        self.bot.exchange = SimpleNamespace(
            fetch_journal_snapshot=Mock(return_value=snapshot()),
            create_order=Mock(side_effect=lambda **kw: {'id': 'ord-ok', 'clientOrderId': kw['client_order_id']}),
        )
        self.bot.has_unresolved_orders = lambda: False
        model = SimpleNamespace(get_trades_proxy=lambda: [], commit=Mock())
        res = journal_create_order(self.bot, None, model, **{**REQUEST, 'amount': 1.0, 'rate': 100.0})
        self.assertEqual(res['id'], 'ord-ok')
        self.bot.exchange.create_order.assert_called_once()
        cid = res['clientOrderId']
        row = self.j.db.execute('SELECT * FROM risk_reservations WHERE client_id=?', (cid,)).fetchone()
        stored = json.loads(row['detail'])
        self.assertEqual(row['state'], 'held')
        self.assertTrue(stored['authorized'])
        # loss_bound must be the complete sum (costs + losses = 3.0), never overwritten by partial loss only
        self.assertAlmostEqual(stored['loss_bound'], 3.0)
        self.assertAlmostEqual(stored['future_cost_bound'], 0.22)
        self.assertAlmostEqual(stored['future_loss_bound'], 2.78)

    def test_validated_v2_budget_denies_slot_cap_aggregate_cap_leverage_missing_mark_price_and_low_headroom(self):
        self.bot.config['prop_risk_budget'] = approved_budget_policy(self.profile)
        self.state['remote_identity_verified'] = True
        self.save()
        # 1. NFIX8 regular entry ($250) + grind/DCA ($50 = $300 total on slot) is AUTHORIZED
        pos_250 = [dict(symbol=PAIR, side='long', amount=2.5, mark_price=100.0)]
        self.j.reconcile(snapshot(positions=pos_250), {}, {(PAIR, 'long'): 2.5})
        dca_decision = require_risk_budget(self.bot, {**REQUEST, 'amount': 0.5, 'rate': 100.0}, 7)
        self.assertTrue(dca_decision['authorized'])
        self.assertAlmostEqual(dca_decision['pair_notional'], 300.0)
        self.j.reconcile(snapshot(), {}, {})
        # 2. Slot cap exceeded (> 750 USDT)
        self.assertEqual(self.deny(amount=8.0, rate=100.0)['reason'], 'slot_notional_cap_exceeded')
        # 3. Leverage != 1.0
        self.assertEqual(self.deny(amount=1.0, rate=100.0, leverage=2.0)['reason'], 'leverage_exceeds_one_step_limit')
        # 4. Insufficient headroom when equity is near floor (headroom = 20 < required 33.0)
        self.state['last_equity'] = 4840
        self.save()
        self.assertEqual(
            self.deny(amount=1.0, rate=100.0)['reason'],
            'insufficient_operating_headroom_for_stress_bound',
        )
        self.state['last_equity'] = 5000
        self.save()
        # 5. Missing mark_price on open position fails closed
        pos_no_mark = [dict(symbol='ETH/USDT:USDT', side='long', amount=1.0)]
        self.j.reconcile(snapshot(positions=pos_no_mark), {}, {('ETH/USDT:USDT', 'long'): 1.0})
        self.assertIn('Missing verified mark_price', self.deny(amount=1.0, rate=100.0)['reason'])
        # 6. Aggregate cap exceeded (> 1200 USDT across positions + request)
        pos_with_mark = [
            dict(symbol='ETH/USDT:USDT', side='long', amount=5.0, mark_price=220.0),
        ]
        self.j.reconcile(snapshot(positions=pos_with_mark), {}, {('ETH/USDT:USDT', 'long'): 5.0})
        self.assertEqual(self.deny(amount=2.0, rate=100.0)['reason'], 'aggregate_notional_cap_exceeded')

    def test_unapproved_model_fails_closed_and_cannot_be_bypassed_by_flag_or_name(self):
        self.state['remote_identity_verified'] = True
        self.save()
        # 1. Default config has no reviewed approval -> strictly denied
        res = self.deny(amount=1.0, rate=100.0)
        self.assertEqual(res['reason'], 'unapproved_risk_budget_parameters')
        self.assertFalse(res['authorized'])

        # 2. Trivial boolean flag cannot bypass
        self.bot.config['prop_risk_budget'] = {
            **self.bot.config.get('prop_risk_budget', {}),
            'approved': True, 'certified': True, 'status': 'approved',
        }
        res2 = self.deny(amount=1.0, rate=100.0)
        self.assertEqual(res2['reason'], 'unapproved_risk_budget_parameters')

        # 3. Mismatched account or challenge in approval contract fails closed
        bad_approval = approved_budget_policy(self.profile)
        bad_approval['approval']['account_id'] = 'different-account'
        self.bot.config['prop_risk_budget'] = bad_approval
        res3 = self.deny(amount=1.0, rate=100.0)
        self.assertEqual(res3['reason'], 'unapproved_risk_budget_parameters')

        # 4. Empty reference in approval contract fails closed
        bad_ref = approved_budget_policy(self.profile)
        bad_ref['approval']['reference'] = '   '
        self.bot.config['prop_risk_budget'] = bad_ref
        res4 = self.deny(amount=1.0, rate=100.0)
        self.assertEqual(res4['reason'], 'unapproved_risk_budget_parameters')

        # 5. Unreviewed approval (reviewed=False) fails closed
        unreviewed = approved_budget_policy(self.profile)
        unreviewed['approval']['reviewed'] = False
        self.bot.config['prop_risk_budget'] = unreviewed
        res5 = self.deny(amount=1.0, rate=100.0)
        self.assertEqual(res5['reason'], 'unapproved_risk_budget_parameters')

    def test_inconsistent_policy_parameters_rejected(self):
        self.state['remote_identity_verified'] = True
        self.save()
        base = approved_budget_policy(self.profile)
        for key, bad_val in [
            ('roundtrip_fee_bps', 5.0),  # < 12 bps
            ('funding_bound_bps', 2.0),  # < 10 bps
            ('execution_slippage_bps', 10.0),  # < 25 bps
            ('latency_gap_stress_bps', 50.0),  # < 200 bps
            ('min_headroom_buffer_usdt', 10.0),  # < 25 USDT
            ('max_slot_notional_usdt', 2000.0),  # > 15% of initial
            ('max_aggregate_notional_usdt', 3000.0),  # > 24% of initial
        ]:
            with self.subTest(key=key, bad_val=bad_val):
                self.bot.config['prop_risk_budget'] = {**base, key: bad_val}
                # This test exercises numeric bounds with a matching review;
                # stale reviews are covered separately by approval regression tests.
                seal_test_policy(self.profile, self.bot.config['prop_risk_budget'], self.config)
                res = self.deny(amount=1.0, rate=100.0)
                self.assertIn('prop_risk_budget policy parameters violate certified safety bounds', res['reason'])

    def test_reduce_only_bypasses_even_when_model_is_unapproved(self):
        # Default config without approval still permits reduceOnly exits unconditionally
        self.bot.config['prop_risk_budget'] = configured_account()['prop_risk_budget']
        require_risk_budget(self.bot, {**REQUEST, 'reduceOnly': True})
        self.assertEqual(self.j.intents(active=False), [])


if __name__ == '__main__':
    unittest.main()
