"""Container preflight before Freqtrade logging, HTTP clients or worker imports."""
from datetime import datetime, timezone
import json
import os
import runpy
import sys
from pathlib import Path

from freqtrade.prop_profile import profile_from_config
from freqtrade.prop_security import (
    DeploymentRejected,
    commission_deployment,
    private_path,
    supervisor_config,
    validate_deployment,
)


def _acquire_verified_flat_baseline(config, profile):
    """Certify 00:00 UTC baseline from Bybit V5 only when account is flat with 0 intraday activity."""
    try:
        import ccxt
        from freqtrade.supervisor_bybit import SupervisorBybit

        exchange = config['exchange']
        api = ccxt.bybit({
            'apiKey': exchange['key'],
            'secret': exchange['secret'],
            'timeout': 2000,
            'enableRateLimit': True,
            'maxRetriesOnFailure': 0,
            'options': {'defaultType': 'swap', 'defaultSubType': 'linear', 'defaultSettle': 'USDT'},
        })
        api.enable_demo_trading(profile['environment'] == 'demo')
        adapter = SupervisorBybit(api, profile)
        with adapter.operation_budget(10.0):
            return adapter.certify_flat_midnight_baseline(datetime.now(timezone.utc))
    except Exception:
        raise DeploymentRejected('Missing or unverifiable baseline record for commissioning') from None


def main():
    role = sys.argv[1]
    default_root = '/freqtrade/user_data' if role == 'core' else '/app/user_data'
    root = Path(os.environ.get('PROP_DATA_DIR') or default_root)
    if role == 'commission' and not root.exists():
        for fallback in (Path('/freqtrade/user_data'), Path(__file__).resolve().parents[1] / 'user_data'):
            if fallback.exists():
                root = fallback
                break
    private_path(root, True)
    path = root / 'config_prop_nfix8.json'
    private_path(path)
    config = supervisor_config(json.loads(path.read_text()))
    config['user_data_dir'] = root
    args = sys.argv[2:]
    for idx, arg in enumerate(args):
        if arg == '--db-url' and idx + 1 < len(args):
            config['db_url'] = args[idx + 1]
        elif arg.startswith('--db-url='):
            config['db_url'] = arg.split('=', 1)[1]
    if role == 'commission':
        baseline_path = root / 'risk_manager_prop_baseline.json'
        if baseline_path.exists():
            private_path(baseline_path)
            baseline = json.loads(baseline_path.read_text(encoding='utf-8'))
        else:
            profile = profile_from_config(config)
            baseline = _acquire_verified_flat_baseline(config, profile)
        commission_deployment(config, config['exchange'], baseline=baseline)
        return
    validate_deployment(config, config['exchange'], role=role)
    if role == 'core':
        # Keep the redaction factory installed before Freqtrade config logging.
        sys.argv = ['freqtrade', *sys.argv[2:]]
        runpy.run_module('freqtrade', run_name='__main__')
    elif role == 'supervisor':
        sys.argv = ['/app/scripts/llm_risk_manager_prop.py']
        runpy.run_path(sys.argv[0], run_name='__main__')
    else:
        raise ValueError('Invalid role')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Do not print exception text, configuration, paths or environment values.
        print('Deployment rejected: explicit credentials, identity and private storage required', file=sys.stderr)
        sys.exit(1)
