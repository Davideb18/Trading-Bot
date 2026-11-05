"""Deterministic, fail-closed prop account guard. No LLM dependencies.

Limits here are internal safety limits, not a certification of exchange rules.
The API adapter must supply full-account equity including unrealized PnL exactly once.
"""

import json
import logging
import math
import os
import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from freqtrade.prop_profile import floors, utc_time, validate_baseline, validate_profile


log = logging.getLogger(__name__)


def finite_number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def market_crash(metrics):
    """Missing prices cannot prove a crash; headlines and flags have no authority."""
    return any(
        finite_number(metrics.get(key)) and metrics[key] <= limit
        for key, limit in (("btc_1h_pct", -4.0), ("btc_24h_pct", -8.0))
    )


def reconciled_state(profile, baseline, now, *, halted):
    """Build a state for explicit offline commissioning, never called by the live guard.

    The operator must reconcile the account and preserve any existing halt before
    installing this record. An absent file is not proof of a fresh account.
    """
    profile = validate_profile(profile)
    validate_baseline(baseline, profile, now)
    if type(halted) is not bool:
        raise ValueError("Explicit reconciled halt status required")
    return {
        "schema_version": 1, "risk_schema_version": 2, "profile": profile,
        "initial_balance": profile["initial_balance"], "baseline": baseline,
        "current_trading_day": now.astimezone(UTC).date().isoformat(),
        "nuclear_triggered": halted, "wake_up_lock": True, "data_fault": True,
        "liquidation_confirmed": False, "last_equity": baseline["equity"],
        "last_updated": now.isoformat(),
    }


class RiskGuard:
    def __init__(self, api, state_file, *, profile, interval=5, baseline_file=None):
        self.profile = validate_profile(profile)
        if not finite_number(interval) or not 0 < interval <= 15:
            raise ValueError("Invalid guard interval")
        self.api_client = api
        self.state_file = Path(state_file)
        self.baseline_file = Path(baseline_file) if baseline_file else self.state_file.with_name(
            "risk_manager_prop_baseline.json"
        )
        self.initial_balance = self.profile["initial_balance"]
        self.interval = interval
        self._state_lock = threading.RLock()
        self._emergency_lock = threading.Lock()
        self._shutdown = threading.Event()
        self.state_error = None
        self.state = {
            # Envelope v1 is retained for the unmodified strategy heartbeat reader.
            # All capital authorization additionally requires risk_schema_version 2.
            "schema_version": 1,
            "risk_schema_version": 2,
            "profile": self.profile,
            "initial_balance": self.initial_balance,
            "baseline": None,
            "current_trading_day": None,
            "nuclear_triggered": False,
            "wake_up_lock": True,
            "data_fault": True,
            "liquidation_confirmed": False,
            "last_equity": self.initial_balance,
            "last_updated": None,
        }
        self._load_state()

    @property
    def nuclear_triggered(self):
        return self.state["nuclear_triggered"]

    def _load_state(self):
        if not self.state_file.exists():
            self.state_error = "Missing state requires explicit offline reconciliation"
            return
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
            if (state.get("schema_version") != 1 or state.get("risk_schema_version") != 2
                    or state.get("profile") != self.profile
                    or state.get("initial_balance") != self.initial_balance):
                raise ValueError("Legacy/mismatched state requires explicit reconciliation")
            for key in ("nuclear_triggered", "wake_up_lock", "data_fault", "liquidation_confirmed"):
                if type(state.get(key)) is not bool:
                    raise ValueError(f"Invalid {key}")
            if not finite_number(state.get("last_equity")):
                raise ValueError("Invalid last_equity")
            updated = utc_time(state["last_updated"])
            if state["baseline"] is not None:
                validate_baseline(state["baseline"], self.profile, updated)
                if state["current_trading_day"] != updated.date().isoformat():
                    raise ValueError("Inconsistent baseline day")
            elif not state["data_fault"] or not state["wake_up_lock"]:
                raise ValueError("Missing baseline in authorized state")
            self.state = state
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            # Preserve evidence. Never silently reset a corrupted or old account baseline.
            self.state_error = str(exc)
            log.critical("State recovery required: %s", exc)

    def _save_state(self):
        if self.state_error:
            return False
        with self._state_lock:
            tmp = self.state_file.with_suffix(".tmp")
            try:
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                with tmp.open("w", encoding="utf-8") as handle:
                    json.dump(self.state, handle, indent=2, allow_nan=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                tmp.replace(self.state_file)
                return True
            except (OSError, ValueError) as exc:
                self.state_error = f"State write failed: {exc}"
                log.critical(self.state_error)
                return False

    def _pause_on_fault(self, reason):
        with self._state_lock:
            self.state["data_fault"] = True
            self.state["wake_up_lock"] = True
            self.state["fault_reason"] = reason
            self._save_state()
        log.error("Entries locked: %s", reason)
        self.api_client.stop_buying()
        # No automatic /start: an operator must reconcile and clear the fault.

    def trigger_nuclear_option(self, reason):
        with self._state_lock:
            now = datetime.now(UTC)
            if self.state["current_trading_day"] is None:
                self.state["current_trading_day"] = now.date().isoformat()
            if self.state["last_updated"] is None:
                self.state["last_updated"] = now.isoformat()
            self.state["nuclear_triggered"] = True
            self.state["wake_up_lock"] = True
            self.state["liquidation_confirmed"] = False
            self.state["halt_reason"] = reason
            self._save_state()
        self._enforce_halt()

    def _enforce_halt(self):
        # Serializes simultaneous crash/DD requests without blocking state reads.
        with self._emergency_lock:
            paused = self.api_client.stop_buying()
            trades = self.api_client.get_open_trades()
            exits_ok = isinstance(trades, list)
            if isinstance(trades, list):
                for trade in trades:
                    trade_id = trade.get("trade_id")
                    if trade_id is None:
                        exits_ok = False
                    else:
                        exits_ok = self.api_client.force_exit(trade_id) and exits_ok
            # Accepted HTTP response does not mean filled. Unknown is not empty.
            remaining = self.api_client.get_open_trades()
            confirmed = paused and exits_ok and remaining == []
            with self._state_lock:
                self.state["liquidation_confirmed"] = confirmed
                self._save_state()
            if not confirmed:
                log.critical("Emergency halt not confirmed; retrying on the next guard cycle")
            return confirmed

    def _check_drawdown(self, now=None):  # noqa: C901 - atomic state update and limits together
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("UTC-aware timestamp required")
        now = now.astimezone(UTC)
        if self.nuclear_triggered:
            self._enforce_halt()
            return True
        if self.state_error:
            self._pause_on_fault(self.state_error)
            return False
        try:
            remote = self.api_client.get_prop_profile()
            if remote != self.profile:
                raise ValueError("Freqtrade/supervisor profile mismatch or unavailable")
            if self.state["last_updated"] and utc_time(self.state["last_updated"]) > now:
                raise ValueError("Clock moved backwards")
        except (ValueError, TypeError, KeyError) as exc:
            self.state_error = str(exc)
            self._pause_on_fault(str(exc))
            return False
        equity = self.api_client.get_equity()
        if not finite_number(equity):
            self._pause_on_fault("Missing/nonfinite full-account equity")
            return False
        baseline = self.state["baseline"]
        baseline_error = None
        try:
            if baseline is None or utc_time(baseline["captured_at"]).date() != now.date():
                baseline = json.loads(self.baseline_file.read_text(encoding="utf-8"))
            validate_baseline(baseline, self.profile, now)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            baseline_error = f"Midnight baseline reconciliation required: {exc}"
            baseline = None
        # Overall protection remains available even while the daily baseline is unknown.
        levels = floors(self.profile, baseline["equity"] if baseline else self.initial_balance)
        reason = None
        if Decimal(str(equity)) <= Decimal(str(levels["operating_overall_floor"])):
            reason = "Internal static overall floor reached"
        if baseline and Decimal(str(equity)) <= Decimal(str(levels["operating_daily_floor"])):
            reason = "Internal static daily floor reached"
        with self._state_lock:
            self.state.update(last_equity=equity, last_updated=now.isoformat(),
                              baseline=baseline, current_trading_day=now.date().isoformat(),
                              data_fault=bool(baseline_error), wake_up_lock=bool(baseline_error))
            self.state.update(levels)
            if not baseline:
                self.state["official_daily_floor"] = None
                self.state["operating_daily_floor"] = None
            if reason:
                self.state.update(nuclear_triggered=True, wake_up_lock=True,
                                  liquidation_confirmed=False, halt_reason=reason)
            saved = self._save_state()
        if not saved:
            self._pause_on_fault(self.state_error)
        if reason:
            self._enforce_halt()
            return True
        if baseline_error:
            self._pause_on_fault(baseline_error)
        return False

    def _handle_shutdown(self, signum, frame):
        self._shutdown.set()
        self._pause_on_fault("Supervisor shutdown")

    def run(self):
        while not self._shutdown.is_set():
            try:
                self._check_drawdown()
            except Exception as exc:
                log.exception("Guard cycle failed")
                try:
                    self._pause_on_fault(str(exc))
                except Exception:
                    log.exception("Unable to pause; entry heartbeat will expire")
            self._shutdown.wait(self.interval)
