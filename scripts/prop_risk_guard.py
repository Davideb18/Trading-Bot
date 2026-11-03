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


log = logging.getLogger(__name__)


def finite_number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def market_crash(metrics):
    """Missing prices cannot prove a crash; headlines and flags have no authority."""
    return any(
        finite_number(metrics.get(key)) and metrics[key] <= limit
        for key, limit in (("btc_1h_pct", -4.0), ("btc_24h_pct", -8.0))
    )


class RiskGuard:
    def __init__(
        self, api, state_file, initial=5000.0, daily_limit=0.040, trailing_limit=0.052, interval=5
    ):
        if (
            not finite_number(initial)
            or initial <= 0
            or not finite_number(daily_limit)
            or not 0 < daily_limit <= 0.045
            or not finite_number(trailing_limit)
            or not 0 < trailing_limit <= 0.090
            or not finite_number(interval)
            or not 0 < interval <= 15
        ):
            raise ValueError("Invalid or unsafe prop guard configuration")
        self.api_client = api
        self.state_file = Path(state_file)
        self.initial_balance = initial
        self.daily_limit = daily_limit
        self.trailing_limit = trailing_limit
        self.interval = interval
        self._state_lock = threading.RLock()
        self._emergency_lock = threading.Lock()
        self._shutdown = threading.Event()
        self.state_error = None
        self.state = {
            "schema_version": 1,
            "initial_balance": initial,
            "peak_balance": initial,
            "day_start_balance": initial,
            "day_peak_balance": initial,
            "current_trading_day": None,
            "nuclear_triggered": False,
            "wake_up_lock": False,
            "data_fault": True,
            "liquidation_confirmed": False,
            "last_equity": initial,
            "last_updated": None,
        }
        self._load_state()

    @property
    def nuclear_triggered(self):
        return self.state["nuclear_triggered"]

    def _load_state(self):
        if not self.state_file.exists():
            return
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
            if state.get("schema_version") != 1:
                raise ValueError("Legacy/unknown state requires explicit reconciliation")
            if state.get("initial_balance") != self.initial_balance:
                raise ValueError("Account allocation changed")
            for key in ("peak_balance", "day_start_balance", "day_peak_balance"):
                if not finite_number(state.get(key)) or state[key] <= 0:
                    raise ValueError(f"Invalid {key}")
            if not finite_number(state.get("last_equity")):
                raise ValueError("Invalid last_equity")
            for key in ("nuclear_triggered", "wake_up_lock", "data_fault", "liquidation_confirmed"):
                if type(state.get(key)) is not bool:
                    raise ValueError(f"Invalid {key}")
            timestamp = datetime.fromisoformat(state["last_updated"])
            if timestamp.tzinfo is None:
                raise ValueError("State timestamp must be timezone-aware")
            datetime.strptime(state["current_trading_day"], "%Y-%m-%d")
            if (
                state["peak_balance"] < self.initial_balance
                or state["peak_balance"] < state["day_peak_balance"]
                or state["day_peak_balance"] < state["day_start_balance"]
            ):
                raise ValueError("Inconsistent high-water marks")
            self.state = state
        except (OSError, ValueError, TypeError, KeyError) as exc:
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
        equity = self.api_client.get_equity()
        if not finite_number(equity):
            self._pause_on_fault("Missing/nonfinite full-account equity")
            return False
        reason = None
        with self._state_lock:
            state = self.state
            today = now.date().isoformat()
            if state["current_trading_day"] is None:
                # Fresh account only. A missing state is never a license to erase losses.
                if equity < self.initial_balance - 0.01:
                    reason = "Missing state for an account with existing profit/loss"
                else:
                    state["peak_balance"] = max(self.initial_balance, equity)
                    state["day_start_balance"] = max(self.initial_balance, equity)
                    state["day_peak_balance"] = max(self.initial_balance, equity)
                state["current_trading_day"] = today
            elif today < state["current_trading_day"]:
                reason = "Clock moved backwards"
            elif today != state["current_trading_day"]:
                # Exact 00:00 equity is unavailable to a polling client. Carry the old
                # sample across the boundary: conservative for the observed endpoints.
                # An unobserved spike still cannot be reconstructed from these samples.
                state["day_start_balance"] = max(state["last_equity"], equity)
                state["day_peak_balance"] = state["day_start_balance"]
                state["current_trading_day"] = today
            state["peak_balance"] = max(state["peak_balance"], equity)
            state["day_peak_balance"] = max(state["day_peak_balance"], equity)
            daily_loss = Decimal(str(state["day_peak_balance"])) - Decimal(str(equity))
            trailing_loss = Decimal(str(state["peak_balance"])) - Decimal(str(equity))
            # Dollar limits based on the original allocation; equality triggers the halt.
            if daily_loss >= Decimal(str(self.initial_balance)) * Decimal(str(self.daily_limit)):
                reason = f"Internal daily loss reached: {daily_loss:.2f} USDT"
            if trailing_loss >= Decimal(str(self.initial_balance)) * Decimal(
                str(self.trailing_limit)
            ):
                reason = f"Internal trailing loss reached: {trailing_loss:.2f} USDT"
            state["last_equity"] = equity
            state["last_updated"] = now.isoformat()
            state["data_fault"] = False
            if not state["nuclear_triggered"]:
                state["wake_up_lock"] = False
            saved = self._save_state()
        if not saved:
            self._pause_on_fault(self.state_error)
        if reason:
            self.trigger_nuclear_option(reason)
            return True
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
