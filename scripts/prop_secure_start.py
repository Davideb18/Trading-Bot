"""Container preflight before Freqtrade logging, HTTP clients or worker imports."""
from datetime import datetime, timezone
import json
import os
import runpy
import sys
from pathlib import Path

from freqtrade.prop_profile import profile_from_config
from freqtrade.prop_security import (
    commission_deployment,
    private_path,
    supervisor_config,
    validate_deployment,
)


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
    if role == 'commission':
        baseline_path = root / 'risk_manager_prop_baseline.json'
        if baseline_path.exists():
            private_path(baseline_path)
            baseline = json.loads(baseline_path.read_text(encoding='utf-8'))
        else:
            now_utc = datetime.now(timezone.utc)
            midnight = datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=timezone.utc)
            profile = profile_from_config(config)
            equity = float(os.environ.get('PROP_COMMISSION_EQUITY', profile['initial_balance']))
            baseline = {
                'schema_version': 1,
                'profile': profile,
                'equity': equity,
                'captured_at': midnight.isoformat(),
                'verified_at': now_utc.isoformat(),
                'source': 'bybit_wallet_coin_equity',
                'confirmed': True,
            }
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
