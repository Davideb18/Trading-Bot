"""Container preflight before Freqtrade logging, HTTP clients or worker imports."""
import json
import runpy
import sys
from pathlib import Path

from freqtrade.prop_security import (
    commission_deployment,
    private_path,
    supervisor_config,
    validate_deployment,
)


def main():
    role = sys.argv[1]
    root = Path('/freqtrade/user_data' if role == 'core' else '/app/user_data')
    private_path(root, True)
    path = root / 'config_prop_nfix8.json'
    private_path(path)
    config = supervisor_config(json.loads(path.read_text()))
    config['user_data_dir'] = root
    if role == 'commission':
        baseline_path = root / 'risk_manager_prop_baseline.json'
        private_path(baseline_path)
        baseline = json.loads(baseline_path.read_text(encoding='utf-8'))
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
