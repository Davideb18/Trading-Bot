"""P0.7 offline admission, network configuration and secret redaction tests."""
import copy
import hashlib
import io
import json
import logging
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import MagicMock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from freqtrade import prop_security as security
from freqtrade.order_journal import OrderJournal, JournalBlocked
from test_prop_profile import configured_account
from freqtrade.prop_profile import profile_from_config
PRIVATE_PATH = security.private_path


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.factory = logging.getLogRecordFactory()
        self.addCleanup(lambda: logging.setLogRecordFactory(self.factory))
        self.addCleanup(patch.stopall)
        patch.object(socket.socket, 'connect', side_effect=AssertionError('No network')).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {name: hashlib.sha256(name.encode()).hexdigest() for name in (
            'PROP_BYBIT_API_KEY', 'PROP_BYBIT_API_SECRET', 'FREQTRADE_PASSWORD',
            'FREQTRADE_JWT_TOKEN', 'FREQTRADE_WS_TOKEN')}
        self.env.update(FREQTRADE_USERNAME='offline-operator',
                        PROP_EMERGENCY_AUTHORITY='dedicated-account-single-host',
                        PROP_PRIVATE_DOCKER_NETWORK='dedicated-bridge',
                        FREQTRADE_API_URL='http://freqtrade_prop:8088/api/v1')
        self.config = security.supervisor_config(configured_account(), self.env)
        self.config['user_data_dir'] = self.root
        self.profile = profile_from_config(self.config)
        for name in ('risk_manager_prop_state.json', 'risk_manager_prop_baseline.json'):
            (self.root / name).write_text(json.dumps({'profile': self.profile}))
        identity = {k: self.profile[k] for k in ('profile_id', 'account_id', 'challenge_id',
                    'phase_id', 'exchange', 'environment', 'currency')}
        identity['key_fingerprint'] = hashlib.sha256(self.env['PROP_BYBIT_API_KEY'].encode()).hexdigest()
        identity['database'] = self.config.get('db_url', 'sqlite:///tradesv3.sqlite')
        self.journal = OrderJournal(self.root / 'order_journal', identity)
        self.addCleanup(self.journal.close)
        self.path_check = patch.object(security, 'private_path').start()
        patch.object(security.os, 'umask').start()
        patch.object(security.os, 'geteuid', return_value=1000, create=True).start()

    def validate(self):
        return security.validate_deployment(self.config, self.config['exchange'], self.env)

    def test_valid_explicit_configuration(self):
        self.assertEqual(self.validate(), self.profile)
        self.path_check.assert_any_call(self.root / 'order_journal' / 'orders.sqlite')

    def test_real_18_char_bybit_api_key_accepted_while_short_secrets_rejected(self):
        key_18 = 'AbCdEfGhIj12345678'
        identity = json.loads(self.journal.db.execute('SELECT value FROM binding').fetchone()[0])
        identity['key_fingerprint'] = hashlib.sha256(key_18.encode()).hexdigest()
        with self.journal.db:
            self.journal.db.execute('UPDATE binding SET value=? WHERE id=1', (json.dumps(identity),))
        self.env['PROP_BYBIT_API_KEY'] = key_18
        self.config['exchange']['key'] = key_18
        self.assertEqual(self.validate(), self.profile)
        short_env = dict(self.env, PROP_BYBIT_API_KEY='AbCdEfGh1234567')
        short_cfg = copy.deepcopy(self.config)
        short_cfg['exchange']['key'] = short_env['PROP_BYBIT_API_KEY']
        with self.assertRaises(security.DeploymentRejected):
            security.validate_deployment(short_cfg, short_cfg['exchange'], short_env)
        short_secret_env = dict(self.env, PROP_BYBIT_API_SECRET='AbCdEfGhIj12345678')
        short_secret_cfg = copy.deepcopy(self.config)
        short_secret_cfg['exchange']['secret'] = short_secret_env['PROP_BYBIT_API_SECRET']
        with self.assertRaises(security.DeploymentRejected):
            security.validate_deployment(short_secret_cfg, short_secret_cfg['exchange'], short_secret_env)

    def test_missing_empty_placeholder_secrets(self):
        for value in (None, '', '   ', 'supersecretjwttoken', 'ZeroBurnPassword2026!', 'a' * 40,
                      'replace-this-password-before-production'):
            with self.subTest(value=value):
                self.config['api_server']['password'] = value
                with self.assertRaises(security.DeploymentRejected) as error:
                    self.validate()
                if value and value.strip():
                    self.assertNotIn(value, str(error.exception))

    def test_identity_mismatch_and_demo_live(self):
        self.config['prop_guard']['account_id'] = 'other-account'
        with self.assertRaises(security.DeploymentRejected):
            self.validate()
        self.config['prop_guard']['account_id'] = self.profile['account_id']
        self.config['exchange']['demo_trading'] = False
        with self.assertRaises(security.DeploymentRejected):
            self.validate()

    def test_supervisor_key_mismatch(self):
        self.env['PROP_BYBIT_API_KEY'] = 'different'
        with self.assertRaises(security.DeploymentRejected):
            self.validate()

    def test_storage_unavailable(self):
        self.path_check.side_effect = security.DeploymentRejected('Protected deployment path unavailable')
        with self.assertRaises(security.DeploymentRejected):
            self.validate()

    def test_second_instance_existing_lock_preserved(self):
        self.validate()
        identity = json.loads(self.journal.db.execute('SELECT value FROM binding').fetchone()[0])
        identity.pop('schema')
        with self.assertRaises(JournalBlocked):
            OrderJournal(self.root / 'order_journal', identity)

    def test_restart_revalidates(self):
        self.validate()
        self.env['PROP_EMERGENCY_AUTHORITY'] = ''
        with self.assertRaises(security.DeploymentRejected):
            self.validate()

    def test_public_host_and_untrusted_url_rejected(self):
        self.env.pop('PROP_PRIVATE_DOCKER_NETWORK')
        with self.assertRaises(security.DeploymentRejected):
            self.validate()
        self.config['api_server']['listen_ip_address'] = '127.0.0.1'
        self.env['FREQTRADE_API_URL'] = 'http://untrusted.invalid:8088/api/v1'
        with self.assertRaises(security.DeploymentRejected):
            self.validate()

    def test_secret_log_and_exception_redacted(self):
        self.validate()
        value = self.env['PROP_BYBIT_API_SECRET']
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger('offline-security')
        logger.addHandler(handler)
        self.addCleanup(lambda: logger.removeHandler(handler))
        try:
            raise ValueError(value)
        except ValueError:
            logger.error('credential=%s', value, exc_info=True)
        self.assertNotIn(value, stream.getvalue())
        self.assertIn('[REDACTED]', stream.getvalue())

    def test_exchange_debug_and_endpoint_overrides_rejected(self):
        for values in ({'verbose': True}, {'urls': {'api': 'other'}}):
            self.config['exchange']['ccxt_config'] = values
            with self.assertRaises(security.DeploymentRejected):
                self.validate()

    def test_compose_private_network_same_volume_and_required_secrets(self):
        import yaml
        root = Path(__file__).resolve().parents[2]
        compose = yaml.safe_load((root / 'docker-compose.prop.yml').read_text())
        core = compose['services']['freqtrade_prop']
        supervisor = compose['services']['freqtrade_prop_risk_manager']
        self.assertEqual(core['ports'], ['127.0.0.1:8088:8088'])
        self.assertNotIn('ports', supervisor)
        self.assertEqual(core['networks'], supervisor['networks'])
        self.assertEqual(core['volumes'][0]['source'], supervisor['volumes'][-1]['source'])
        self.assertFalse(core['volumes'][0]['bind']['create_host_path'])
        for service in (core, supervisor):
            for key, value in service['environment'].items():
                if any(s in key for s in ('PASSWORD', 'SECRET', 'TOKEN', 'BYBIT_API_KEY')):
                    self.assertIn(':?', value)

    def test_actual_permission_function_rejects_windows_acl_uncertainty(self):
        if os.name == 'nt':
            with self.assertRaises(security.DeploymentRejected):
                PRIVATE_PATH(self.root, True)

    def test_posix_permissions_owner_and_links(self):
        import stat
        path = MagicMock()
        path.absolute.return_value = path
        path.parents = []
        path.is_symlink.return_value = False
        info = SimpleNamespace(st_uid=1000, st_mode=stat.S_IFREG | 0o600)
        path.stat.return_value = info
        fake_os = SimpleNamespace(name='posix', geteuid=lambda: 1000,
                                  access=lambda *args: True, R_OK=4, W_OK=2, X_OK=1)
        with patch.object(security, 'Path', return_value=path), patch.object(security, 'os', fake_os):
            PRIVATE_PATH('offline')
            for uid, mode, link in ((1001, 0o600, False), (1000, 0o644, False), (1000, 0o600, True)):
                info.st_uid, info.st_mode = uid, stat.S_IFREG | mode
                path.is_symlink.return_value = link
                with self.assertRaises(security.DeploymentRejected):
                    PRIVATE_PATH('offline')

    def test_preflight_before_exchange_construction(self):
        import ast
        root = Path(__file__).resolve().parents[2]
        tree = ast.parse((root / 'freqtrade/freqtradebot.py').read_text())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        validation = next(n.lineno for n in calls if isinstance(n.func, ast.Name) and n.func.id == 'validate_deployment')
        exchange = next(n.lineno for n in calls if isinstance(n.func, ast.Attribute) and n.func.attr == 'load_exchange')
        self.assertLess(validation, exchange)

    def test_container_preflight_runs_before_each_service_in_same_process(self):
        import prop_secure_start as start
        (self.root / 'config_prop_nfix8.json').write_text(json.dumps(configured_account()))
        for role in ('core', 'supervisor'):
            with self.subTest(role=role), patch.object(start, 'Path', return_value=self.root), \
                    patch.object(start, 'private_path'), patch.object(start, 'validate_deployment') as check, \
                    patch.object(start.runpy, 'run_module') as core, \
                    patch.object(start.runpy, 'run_path') as supervisor, \
                    patch.object(sys, 'argv', ['preflight', role, 'trade']), patch.dict(os.environ, self.env, clear=True):
                def launched(*args, **kwargs):
                    check.assert_called_once()
                    self.assertEqual(check.call_args.kwargs.get('role'), role)
                core.side_effect = supervisor.side_effect = launched
                start.main()
                self.assertEqual(core.call_count + supervisor.call_count, 1)

    def test_compose_core_and_supervisor_env_parity_and_freqtrade_env_fallback(self):
        import yaml
        root = Path(__file__).resolve().parents[2]
        compose = yaml.safe_load((root / 'docker-compose.prop.yml').read_text())
        core_env = compose['services']['freqtrade_prop']['environment']
        sup_env = compose['services']['freqtrade_prop_risk_manager']['environment']
        for key in ('FREQTRADE_JWT_TOKEN', 'FREQTRADE_WS_TOKEN', 'FREQTRADE_USERNAME',
                    'FREQTRADE_PASSWORD', 'PROP_BYBIT_API_KEY', 'PROP_BYBIT_API_SECRET'):
            self.assertIn(key, core_env)
            self.assertIn(key, sup_env)
        ft_only_env = {
            'PROP_BYBIT_API_KEY': self.env['PROP_BYBIT_API_KEY'],
            'PROP_BYBIT_API_SECRET': self.env['PROP_BYBIT_API_SECRET'],
            'FREQTRADE_USERNAME': self.env['FREQTRADE_USERNAME'],
            'FREQTRADE_PASSWORD': self.env['FREQTRADE_PASSWORD'],
            'FREQTRADE__API_SERVER__JWT_SECRET_KEY': self.env['FREQTRADE_JWT_TOKEN'],
            'FREQTRADE__API_SERVER__WS_TOKEN': self.env['FREQTRADE_WS_TOKEN'],
            'PROP_EMERGENCY_AUTHORITY': 'dedicated-account-single-host',
            'PROP_PRIVATE_DOCKER_NETWORK': 'dedicated-bridge',
            'FREQTRADE_API_URL': 'http://freqtrade_prop:8088/api/v1',
        }
        resolved = security.supervisor_config(configured_account(), ft_only_env)
        resolved['user_data_dir'] = self.root
        self.assertEqual(security.validate_deployment(resolved, resolved['exchange'], ft_only_env), self.profile)
        bad_env = dict(ft_only_env, FREQTRADE__API_SERVER__JWT_SECRET_KEY='mismatched-token')
        with self.assertRaises(security.DeploymentRejected):
            security.validate_deployment(resolved, resolved['exchange'], bad_env)

    def test_supervisor_role_starts_when_state_or_baseline_missing_or_corrupt_but_core_rejects(self):
        state_file = self.root / 'risk_manager_prop_state.json'
        baseline_file = self.root / 'risk_manager_prop_baseline.json'
        state_file.write_text('{corrupt-json')
        with self.assertRaises(security.DeploymentRejected):
            security.validate_deployment(self.config, self.config['exchange'], self.env, role='core')
        self.assertEqual(
            security.validate_deployment(self.config, self.config['exchange'], self.env, role='supervisor'),
            self.profile,
        )
        state_file.unlink()
        baseline_file.unlink()
        with self.assertRaises(security.DeploymentRejected):
            security.validate_deployment(self.config, self.config['exchange'], self.env, role='core')
        self.assertEqual(
            security.validate_deployment(self.config, self.config['exchange'], self.env, role='supervisor'),
            self.profile,
        )
        foreign = copy.deepcopy(self.profile)
        foreign['account_id'] = 'foreign-account'
        state_file.write_text(json.dumps({'profile': foreign}))
        with self.assertRaises(security.DeploymentRejected):
            security.validate_deployment(self.config, self.config['exchange'], self.env, role='supervisor')

    def test_commission_deployment_bootstraps_fresh_volume_and_refuses_overwrite(self):
        from datetime import datetime, timezone
        from test_prop_profile import baseline as make_baseline
        now = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
        fresh = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(fresh, ignore_errors=True))
        (fresh / 'config_prop_nfix8.json').write_text(json.dumps(configured_account()))
        cfg = copy.deepcopy(self.config)
        cfg['user_data_dir'] = fresh
        b = make_baseline(self.profile, equity=5000, now=now)
        commissioned = security.commission_deployment(
            cfg, cfg['exchange'], baseline=b, now=now, halted=False, environ=self.env
        )
        self.assertEqual(commissioned, self.profile)
        self.assertTrue((fresh / 'risk_manager_prop_state.json').is_file())
        self.assertTrue((fresh / 'risk_manager_prop_baseline.json').is_file())
        self.assertTrue((fresh / 'order_journal' / 'orders.sqlite').is_file())
        self.assertEqual(
            security.validate_deployment(cfg, cfg['exchange'], self.env, role='core'),
            self.profile,
        )
        from freqtrade.journal_execution import open_journal
        core_journal = open_journal(cfg, cfg['exchange'])
        self.assertIsNotNone(core_journal)
        core_journal.close()
        with self.assertRaises(security.DeploymentRejected):
            security.commission_deployment(
                cfg, cfg['exchange'], baseline=b, now=now, halted=False, environ=self.env
            )

    def test_secure_start_commission_role_rejects_missing_baseline(self):
        import prop_secure_start as start
        fresh = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(fresh, ignore_errors=True))
        os.chmod(fresh, 0o700)
        cfg_file = fresh / 'config_prop_nfix8.json'
        cfg_file.write_text(json.dumps(configured_account()))
        os.chmod(cfg_file, 0o600)
        env = dict(self.env, PROP_DATA_DIR=str(fresh))
        with patch.object(sys, 'argv', ['preflight', 'commission']), patch.dict(os.environ, env, clear=True):
            with self.assertRaises(security.DeploymentRejected):
                start.main()

    def test_commission_cannot_auto_certify_a_flat_wallet(self):
        import prop_secure_start as start
        with self.assertRaises(security.DeploymentRejected):
            start._acquire_verified_flat_baseline(self.config, self.profile)


if __name__ == '__main__':
    unittest.main()
