"""Offline deployment admission checks. Does not grant trading authority."""
import hashlib
import json
import logging
import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from urllib.parse import quote, quote_plus, urlsplit


class DeploymentRejected(ValueError):
    pass


def secret(value, label):
    bad = ('password', 'changeme', 'change-me', 'placeholder', 'supersecret',
           'zeroburn', 'example', 'your_', 'your-', 'replace', 'dummy', 'test', 'secret')
    if (not isinstance(value, str) or value != value.strip() or len(value) < 24
            or len(set(value)) < 8 or any(s in value.lower() for s in bad)):
        raise DeploymentRejected('Missing or unsafe ' + label)
    return value


def redact_logs(values):
    """Redact known secrets before any handler, including encoded HTTP values.

    Suppress exception text: third-party exceptions may embed signed requests.
    This cannot sanitize output written directly to stdout by external code.
    """
    tokens = sorted({v for value in values if isinstance(value, str) and value
                     for v in (value, quote(value, safe=''), quote_plus(value))}, key=len, reverse=True)
    previous = logging.getLogRecordFactory()
    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        message = record.getMessage()
        for token in tokens:
            message = message.replace(token, '[REDACTED]')
        if record.exc_info:
            message += ' [exception details suppressed]'
        record.msg, record.args = message, ()
        record.exc_info = record.exc_text = record.stack_info = None
        return record
    logging.setLogRecordFactory(factory)


def private_path(path, directory=False):
    """Linux deployment only: reject links, foreign owners and group/world access.

    Windows ACLs cannot be inferred from POSIX mode bits; refuse rather than
    claim to have verified them. Offline unit tests may mock filesystem metadata.
    """
    path = Path(path).absolute()
    if os.name != 'posix':
        raise DeploymentRejected('Operational deployment requires verified POSIX permissions')
    try:
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise DeploymentRejected('Symlink in protected deployment path')
        info = path.stat()
        if (info.st_uid != os.geteuid() or info.st_mode & 0o077
                or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
                or not os.access(path, os.R_OK | os.W_OK | (os.X_OK if directory else 0))):
            raise DeploymentRejected('Protected deployment path has unsafe ownership or permissions')
    except OSError:
        raise DeploymentRejected('Protected deployment path unavailable') from None


def validate_deployment(config, exchange, environ=None):
    """Called before exchange construction. Never reads or changes a user .env."""
    from freqtrade.prop_profile import profile_from_config
    env = os.environ if environ is None else environ
    if os.name == 'posix' and os.geteuid() == 0:
        raise DeploymentRejected('Non-root deployment user required')
    api = config.get('api_server', {})
    values = [exchange.get('key'), exchange.get('secret'), api.get('password'),
              api.get('jwt_secret_key'), api.get('ws_token'), env.get('GEMINI_API_KEY')]
    redact_logs(values)
    try:
        profile = profile_from_config(config)
    except Exception:
        raise DeploymentRejected('Unverifiable deployment profile') from None
    for value, label in zip(values[:5], ('exchange key', 'exchange secret', 'API password', 'JWT key', 'WS token')):
        secret(value, label)
    if len(set(values[:5])) != 5:
        raise DeploymentRejected('Deployment secrets must be distinct')
    if not isinstance(api.get('username'), str) or not api['username'].strip():
        raise DeploymentRejected('Explicit API username required')
    if (env.get('PROP_EMERGENCY_AUTHORITY') != 'dedicated-account-single-host'
            or env.get('PROP_LOAD_ENV_FILE') == '1'):
        raise DeploymentRejected('Dedicated shared-host deployment required; implicit env loading forbidden')
    if (api.get('enabled') is not True or api.get('enable_openapi') is not False
            or api.get('CORS_origins') != [] or exchange.get('log_responses')
            or any(exchange.get(k, {}).get('verbose') for k in ('ccxt_config', 'ccxt_sync_config', 'ccxt_async_config'))):
        raise DeploymentRejected('Unsafe API or exchange logging configuration')
    host = api.get('listen_ip_address')
    if host not in ('127.0.0.1', '::1') and not (
            host == '0.0.0.0' and env.get('PROP_PRIVATE_DOCKER_NETWORK') == 'dedicated-bridge'):
        raise DeploymentRejected('Public API binding not authorized')
    endpoint = urlsplit(env.get('FREQTRADE_API_URL', 'http://127.0.0.1:8088/api/v1'))
    if (endpoint.scheme != 'http' or endpoint.hostname not in ('127.0.0.1', 'localhost', 'freqtrade_prop')
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
            or endpoint.port != 8088 or endpoint.path != '/api/v1'):
        raise DeploymentRejected('Untrusted control API endpoint')
    for field, name in (('key', 'PROP_BYBIT_API_KEY'), ('secret', 'PROP_BYBIT_API_SECRET')):
        if exchange[field] != env.get(name):
            raise DeploymentRejected('Core/supervisor credential mismatch')
    for field, name in (('username', 'FREQTRADE_USERNAME'), ('password', 'FREQTRADE_PASSWORD')):
        if api[field] != env.get(name):
            raise DeploymentRejected('Core/supervisor API credential mismatch')
    for key in ('ccxt_config', 'ccxt_sync_config', 'ccxt_async_config'):
        override = exchange.get(key, {})
        if any(k in override for k in ('apiKey', 'secret', 'urls', 'hostname', 'headers')):
            raise DeploymentRejected('Exchange identity/endpoint overrides forbidden')
    root = Path(config['user_data_dir']).absolute()
    private_path(root, True)
    for path in (root / 'logs', root / 'trades_prop.sqlite'):
        if path.exists() or path.is_symlink():
            private_path(path, path.name == 'logs')
    if (root / 'logs').is_dir():
        for path in (root / 'logs').iterdir():
            private_path(path)
    for name in ('risk_manager_prop_state.json', 'risk_manager_prop_baseline.json', 'config_prop_nfix8.json'):
        private_path(root / name)
    journal = root / 'order_journal'
    private_path(journal, True)
    for path in journal.iterdir():
        private_path(path)
    private_path(journal / 'orders.sqlite')
    try:
        state = json.loads((root / 'risk_manager_prop_state.json').read_text())
        baseline = json.loads((root / 'risk_manager_prop_baseline.json').read_text())
        if state['profile'] != profile or baseline['profile'] != profile:
            raise ValueError()
        with closing(sqlite3.connect((journal / 'orders.sqlite').as_uri() + '?mode=ro', uri=True, timeout=.05)) as db:
            binding = json.loads(db.execute('SELECT value FROM binding WHERE id=1').fetchone()[0])
        if (any(binding.get(k) != profile[k] for k in (
                'profile_id', 'account_id', 'challenge_id', 'phase_id', 'exchange', 'environment', 'currency'))
                or binding.get('key_fingerprint') != hashlib.sha256(exchange['key'].encode()).hexdigest()):
            raise ValueError()
    except Exception:
        raise DeploymentRejected('Unverifiable state or journal identity') from None
    # Existing lifetime locks remain the only execution coordination mechanism.
    # Restrict permissions of subsequently created state, journal and log files.
    os.umask(0o077)
    return profile


def supervisor_config(config, environ=None):
    """Resolve exactly the same explicit deployment values as Docker's core env."""
    import copy
    env = os.environ if environ is None else environ
    config = copy.deepcopy(config)
    config['exchange'].update(key=env.get('PROP_BYBIT_API_KEY'), secret=env.get('PROP_BYBIT_API_SECRET'))
    config['api_server'].update(username=env.get('FREQTRADE_USERNAME'), password=env.get('FREQTRADE_PASSWORD'),
        jwt_secret_key=env.get('FREQTRADE_JWT_TOKEN'), ws_token=env.get('FREQTRADE_WS_TOKEN'))
    if env.get('PROP_PRIVATE_DOCKER_NETWORK') == 'dedicated-bridge':
        config['api_server']['listen_ip_address'] = '0.0.0.0'
    return config
