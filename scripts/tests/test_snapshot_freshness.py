"""Offline temporal provenance and journal/admission integration regressions."""
import copy
import json
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_order_journal as journal_fixtures
import test_prop_risk_approval as approval_fixtures
import test_prop_emergency as emergency_fixtures
import test_prop_rollover as rollover_fixtures
from test_order_journal import REQUEST, PAIR, snapshot, order, local
from snapshot_fixtures import reads
from freqtrade.order_journal import OrderJournal
from freqtrade.prop_risk_budget import require_risk_budget
from freqtrade.snapshot_freshness import validate_reads, validate_snapshot


class FreshnessTests(unittest.TestCase):
    setUp = approval_fixtures.ApprovalTests.setUp
    save = approval_fixtures.ApprovalTests.save
    deny = approval_fixtures.ApprovalTests.deny
    decision = approval_fixtures.ApprovalTests.decision

    def old(self, record):
        for k in ('started_ms', 'received_ms', 'server_ms'):
            record[k] -= 16000

    def test_recent_completion_does_not_refresh_old_positions(self):
        s = snapshot()
        self.old(s['reads'][1])
        self.assertTrue(self.j.reconcile(s, {}, {}))
        self.assertFalse(self.j.ready)
        self.assertIsNone(self.j.risk_snapshot)
        self.assertFalse(self.deny()['authorized'])

    def test_old_mark_with_fresh_positions_blocks_only_admission(self):
        s = snapshot(positions=[dict(symbol=PAIR, side='long', amount=1., mark_price=100.)])
        s['positions'][0]['mark_price_observed_ms'] -= 16000
        self.assertEqual(self.j.reconcile(s, {}, {(PAIR, 'long'): 1.}), [])
        self.assertIn('mark price', self.deny()['reason'])
        require_risk_budget(self.bot, {**REQUEST, 'reduceOnly': True})

    def test_missing_mark_timestamp_never_uses_position_update_time(self):
        s = snapshot(positions=[dict(symbol=PAIR, side='long', amount=1., mark_price=100.)])
        del s['positions'][0]['mark_price_observed_ms']
        s['positions'][0]['updatedTime'] = time.time() * 1000
        self.j.reconcile(s, {}, {(PAIR, 'long'): 1.})
        self.assertIn('mark price', self.deny()['reason'])

    def test_old_equity_or_identity_not_refreshed_by_state_save(self):
        original = copy.deepcopy(self.state['risk_component_reads'])
        for component in ('equity', 'identity', 'account_mode'):
            with self.subTest(component=component):
                self.state['risk_component_reads'] = copy.deepcopy(original)
                self.old(next(r for r in self.state['risk_component_reads'] if r['component'] == component))
                self.save()
                self.assertFalse(self.deny()['authorized'])

    def test_missing_remote_time_or_component(self):
        for key in ('server_ms', 'started_ms', 'received_ms', 'elapsed_ms'):
            with self.subTest(key=key):
                s = snapshot()
                del s['reads'][0][key]
                self.assertTrue(self.j.reconcile(s, {}, {}))
        s = snapshot()
        s['reads'] = s['reads'][1:]
        self.assertTrue(self.j.reconcile(s, {}, {}))

    def test_server_clock_skew_and_cached_response(self):
        for offset in (-16000, -3000, 3000):
            with self.subTest(offset=offset):
                s = snapshot()
                s['reads'][0]['server_ms'] += offset
                self.assertTrue(self.j.reconcile(s, {}, {}))

    def test_wall_clock_jump_disagrees_with_monotonic_duration(self):
        r = reads('positions')[0]
        r['started_ms'] -= 4000
        with self.assertRaises(ValueError):
            validate_reads([r])

    def test_missing_or_incomplete_snapshot_does_not_release(self):
        cid = self.j.prepare(REQUEST)
        o = order(cid, status='canceled')
        for change in ({'complete': False}, {'reads': []}):
            s = {**snapshot(o), **change}
            self.assertTrue(self.j.reconcile(s, {}, {}))
            self.assertEqual(self.j.intents()[0]['settled'], 0)
            self.assertEqual(self.j.db.execute('SELECT state FROM risk_reservations').fetchone()[0], 'held')

    def test_expiry_during_reconciliation_rolls_back_settlement(self):
        cid = self.j.prepare(REQUEST)
        o = order(cid, status='canceled')
        s = snapshot(o)
        now = time.time() * 1000
        with patch('freqtrade.snapshot_freshness.milliseconds', side_effect=[now, now + 16000]):
            self.assertTrue(self.j.reconcile(s, {}, {}))
        self.assertEqual(self.j.intents()[0]['settled'], 0)
        self.assertEqual(self.j.db.execute('SELECT state FROM risk_reservations').fetchone()[0], 'held')
        self.assertEqual(self.j.reconcile(snapshot(o), {}, {}), [])
        self.assertEqual(self.j.db.execute('SELECT state FROM risk_reservations').fetchone()[0], 'reconciled')

    def test_gate_rechecks_snapshot_even_if_completion_stamp_is_fresh(self):
        self.assertTrue(self.decision()['authorized'])
        self.old(self.j.risk_snapshot['reads'][1])
        self.j.last_reconciliation['checked_ms'] = int(time.time() * 1000)
        self.assertFalse(self.deny()['authorized'])

    def test_restart_does_not_reuse_previous_snapshot(self):
        self.j.close()
        self.j = OrderJournal(Path(self.tmp.name) / 'order_journal', self.identity)
        self.bot.order_journal = self.j
        self.assertIsNone(self.j.risk_snapshot)
        self.assertFalse(self.deny()['authorized'])

    def test_admission_records_original_component_intervals(self):
        decision = self.decision()
        cid = self.j.prepare(REQUEST, reservation=decision)
        stored = json.loads(self.j.db.execute('SELECT detail FROM risk_reservations WHERE client_id=?', (cid,)).fetchone()[0])
        self.assertEqual(stored['risk_component_reads'], decision['risk_component_reads'])
        self.assertIn('equity', {r['component'] for r in stored['risk_component_reads']})

    def test_control_ack_does_not_use_recent_completion_for_old_reads(self):
        from freqtrade.prop_controls import control_status
        from types import SimpleNamespace
        self.old(self.j.risk_snapshot['reads'][0])
        status = control_status(self.bot,
            SimpleNamespace(get_open_trades=lambda: []),
            SimpleNamespace(get_open_orders=lambda: []))
        self.assertFalse(status['verified'])
        self.assertFalse(status['flat'])

    def test_emergency_remains_available_after_stale_snapshot(self):
        self.old(self.j.risk_snapshot['reads'][0])
        self.assertFalse(self.deny()['authorized'])
        with self.j.db:
            self.j.db.execute('INSERT INTO emergency VALUES (1,?)', (json.dumps(dict(
                owner='offline', handoff=True, phase='liquidating', halt_id='test')),))
        request = {**REQUEST, 'reduceOnly': True, 'side': 'sell', 'ordertype': 'market'}
        cid = self.j.prepare_emergency(request, 'offline')
        send = Mock(return_value={'id': 'offline-exit', 'clientOrderId': cid})
        self.j.dispatch(cid, send)
        send.assert_called_once()


class AdapterFreshnessTests(unittest.TestCase):
    setUp = journal_fixtures.AdapterTests.setUp
    setup_snapshot = journal_fixtures.AdapterTests.setup_snapshot
    page = staticmethod(journal_fixtures.AdapterTests.page)

    def test_adapter_records_all_pages_including_execution_and_bookends(self):
        _, _, intents = self.setup_snapshot()
        s = self.a.fetch_journal_snapshot(intents)
        self.assertEqual(len(s['reads']), 7)
        self.assertEqual({r['component'] for r in s['reads']}, {
            'open_orders', 'positions', 'open_orders_end', 'positions_end',
            'realtime:ftj-test', 'history:ftj-test', 'executions:ftj-test'})
        validate_snapshot(s)

    def test_missing_server_timestamp_rejected_by_real_adapter(self):
        api, _, intents = self.setup_snapshot()
        del api.privateGetV5PositionList.return_value['time']
        with self.assertRaises(ValueError):
            self.a.fetch_journal_snapshot(intents)

    def test_long_pagination_cannot_refresh_initial_page(self):
        self.setup_snapshot()
        first = self.page([], 'next')
        now = time.time() * 1000
        first['time'] = now
        def endpoint(params):
            if 'cursor' not in params:
                return first
            # The individual response is recent, but the first page is now old.
            return {**self.page([]), 'time': now + 16001}
        ticks = iter([now, now, now + 16001, now + 16001])
        observations = []
        with patch('freqtrade.snapshot_freshness.milliseconds', side_effect=lambda: next(ticks, now + 16001)):
            self.a._journal_pages(endpoint, {}, observations, 'open_orders')
            self.assertEqual(len(observations), 2)
            with self.assertRaisesRegex(ValueError, 'Stale'):
                validate_reads(observations)

    def test_fill_during_reads_detected_by_position_bookend(self):
        api, _, intents = self.setup_snapshot()
        api.privateGetV5PositionList.side_effect = [self.page([]), self.page([
            dict(symbol='BTCUSDT', side='Buy', size='2', positionIdx=0, tradeMode=1)])]
        with self.assertRaisesRegex(ValueError, 'Account changed'):
            self.a.fetch_journal_snapshot(intents)

    def test_open_order_change_during_reads_is_not_atomic_snapshot(self):
        api, raw, intents = self.setup_snapshot()
        api.privateGetV5OrderRealtime.side_effect = [self.page([]), self.page([]), self.page([raw])]
        with self.assertRaisesRegex(ValueError, 'Account changed'):
            self.a.fetch_journal_snapshot(intents)


class SupervisorFreshnessTests(unittest.TestCase):
    setUp = emergency_fixtures.AdapterTests.setUp
    response = emergency_fixtures.AdapterTests.response
    page = emergency_fixtures.AdapterTests.page

    def test_independent_facade_preserves_real_call_intervals(self):
        from prop_independent import IndependentRiskAPI
        facade = IndependentRiskAPI(self.adapter, Mock())
        self.assertEqual(facade.get_prop_profile(), self.profile)
        self.assertEqual(facade.get_equity(), 4820)
        validate_reads(facade.identity_reads + [facade.equity_observation['read']],
                       required=('identity', 'account_mode', 'equity'))
        self.assertEqual(facade.equity_observation['read']['server_ms'],
                         self.api.privateGetV5AccountWalletBalance.return_value['time'])

    def test_supervisor_snapshot_rejects_expired_initial_component(self):
        from freqtrade.supervisor_bybit import UnverifiedAccount
        original = self.adapter._snapshot
        def delayed():
            result = original()
            for key in ('started_ms', 'received_ms', 'server_ms'):
                self.adapter._snapshot_reads[0][key] -= 16000
            return result
        with patch.object(self.adapter, '_snapshot', side_effect=delayed):
            with self.assertRaises(UnverifiedAccount):
                self.adapter.snapshot()
        self.assertIsNone(self.adapter._snapshot_reads)


class RiskStateProvenanceTests(unittest.TestCase):
    setUp = rollover_fixtures.RolloverTests.setUp

    def test_guard_persists_remote_provenance_without_retimestamping(self):
        identity = reads('identity', 'account_mode')
        equity = reads('equity')[0]
        self.direct.identity_reads = identity
        self.direct.equity_observation = dict(equity=5000., read=equity,
            response_time_ms=equity['server_ms'], source='offline')
        self.guard._check_drawdown(self.before)
        stored = json.loads(self.path.read_text())
        self.assertEqual(stored['risk_component_reads'], identity + [equity])
        self.direct.equity_observation = None
        self.direct.identity_reads = []
        self.guard._check_drawdown(self.before)
        self.assertEqual(self.guard.state['risk_component_reads'], [])


if __name__ == '__main__':
    unittest.main()
