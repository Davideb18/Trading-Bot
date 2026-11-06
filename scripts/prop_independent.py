"""Runtime wiring for direct risk reads; ordinary trading remains in Freqtrade."""

import os
from contextlib import contextmanager

from freqtrade.emergency_supervisor import EmergencySupervisor
from freqtrade.supervisor_bybit import AccountMismatch, SupervisorBybit, UnverifiedAccount


class IndependentRiskAPI:
    def __init__(self, direct, control):
        self.direct, self.control = direct, control

    @contextmanager
    def operation_budget(self, seconds=4):
        with self.direct.operation_budget(seconds), self.control.operation_budget(seconds):
            yield

    def deadline_expired(self):
        return self.control.deadline_expired()

    def get_prop_profile(self):
        try:
            return self.direct.verify_identity()
        except AccountMismatch:
            raise
        except (KeyError, ValueError, TypeError):
            raise UnverifiedAccount("Incomplete account identity response") from None

    def get_equity(self):
        try:
            return self.direct.equity()
        except (UnverifiedAccount, KeyError, ValueError, TypeError):
            return None

    def __getattr__(self, name):
        # These calls only coordinate ordinary operation. Emergency execution
        # never uses this delegate, /status, /forceexit, /balance, or the ORM.
        return getattr(self.control, name)


def independent_supervisor(profile, state_file, control):
    import ccxt

    key, secret = os.environ.get("PROP_BYBIT_API_KEY"), os.environ.get("PROP_BYBIT_API_SECRET")
    if not key or not secret:
        raise ValueError("Dedicated supervisor credentials must be explicitly configured")
    api = ccxt.bybit({"apiKey": key, "secret": secret, "timeout": 2000,
                     "enableRateLimit": True, "maxRetriesOnFailure": 0,
                     "options": {"defaultType": "swap", "defaultSubType": "linear", "defaultSettle": "USDT"}})
    api.enable_demo_trading(profile["environment"] == "demo")  # Local URL configuration only.
    direct = SupervisorBybit(api, profile)
    emergency = EmergencySupervisor(direct, state_file.parent / "order_journal", state_file,
        profile, key, coordinated=os.environ.get("PROP_EMERGENCY_AUTHORITY") == "dedicated-account-single-host")
    return IndependentRiskAPI(direct, control), emergency
