"""Deterministic, fail-closed prop account guard. No LLM dependencies.

Limits here are internal safety limits, not a certification of exchange rules.
The API adapter must supply full-account equity including unrealized PnL exactly once.
"""

import json
import logging
import math
import os
import threading
import time
from contextlib import nullcontext
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TypeGuard

from freqtrade.prop_profile import floors, utc_time, validate_baseline, validate_profile
from freqtrade.prop_controls import publish_fault_event
from freqtrade.supervisor_bybit import UnverifiedAccount


log = logging.getLogger(__name__)


def finite_number(value) -> TypeGuard[float]:
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
        "initial_balance": profile["initial_balance"], "baseline": baseline, "baseline_status": "verified",
        "current_trading_day": now.astimezone(UTC).date().isoformat(),
        "nuclear_triggered": halted, "wake_up_lock": True, "data_fault": False,
        "control_schema_version": 1, "macro_paused": False, "data_valid": False,
        "fault_revision": 0, "acknowledged_control": None,
        "liquidation_confirmed": False, "last_equity": baseline["equity"],
        "last_updated": now.isoformat(),
    }


class RiskGuard:
    def __init__(self, api, state_file, *, profile, interval=5, baseline_file=None, emergency=None):
        self.profile = validate_profile(profile)
        if not finite_number(interval) or not 0 < interval <= 15:
            raise ValueError("Invalid guard interval")
        self.api_client = api
        self.emergency = emergency
        self.state_file = Path(state_file)
        self.baseline_file = Path(baseline_file) if baseline_file else self.state_file.with_name(
            "risk_manager_prop_baseline.json"
        )
        self.initial_balance = self.profile["initial_balance"]
        self.interval = interval
        self._state_lock = threading.RLock()
        self._emergency_lock = threading.Lock()
        self._shutdown = threading.Event()
        self._pending_macro = None
        self._pending_fault = False
        self.state_error = None
        self.state = {
            # Envelope v1 is retained for the unmodified strategy heartbeat reader.
            # All capital authorization additionally requires risk_schema_version 2.
            "control_schema_version": 1,
            "macro_paused": False, "data_valid": False, "fault_revision": 0,
            "acknowledged_control": None,
            "schema_version": 1,
            "risk_schema_version": 2,
            "profile": self.profile,
            "initial_balance": self.initial_balance,
            "baseline": None, "baseline_status": "missing", "monitoring_status": "unavailable",
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
            state.setdefault("control_schema_version", 1)
            state.setdefault("macro_paused", False)
            state.setdefault("data_valid", False)
            state.setdefault("fault_revision", 0)
            state.setdefault("acknowledged_control", None)
            self.state = state
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            # Preserve evidence. Never silently reset a corrupted or old account baseline.
            self.state["baseline_status"] = "unverified"
            self.state_error = str(exc)
            log.critical("State recovery required: %s", exc)

    def _save_state(self, *, force_emergency=False):
        if self.state_error and not force_emergency:
            return False
        # Only the risk worker writes this file. Workers publish memory-only
        # requests. Never hold the risk-state mutex across filesystem I/O.
        with self._state_lock:
            snapshot = deepcopy(self.state)
        tmp = self.state_file.with_suffix(".tmp")
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(snapshot, handle, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(self.state_file)
            return True
        except (OSError, ValueError) as exc:
            self.state_error = f"State write failed: {exc}"
            log.critical(self.state_error)
            return False

    def _save_baseline(self, baseline):
        tmp = self.baseline_file.with_suffix(".tmp")
        try:
            self.baseline_file.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(baseline, handle, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(self.baseline_file)
            return True
        except (OSError, ValueError) as exc:
            log.critical("Baseline write failed: %s", exc)
            return False

    def _can_certify_midnight_rollover(self, now):
        # Polling never certifies the unobserved instant between observations.
        return False

    def _persist_emergency_halt(self, now, equity, levels, reason):
        if self.state_file.exists():
            backup = self.state_file.with_suffix(".corrupt.json")
            if not backup.exists():
                try:
                    backup.write_bytes(self.state_file.read_bytes())
                except OSError:
                    pass
        with self._state_lock:
            self.state.update(
                current_trading_day=now.date().isoformat(),
                last_updated=now.isoformat(),
                last_equity=float(equity),
                nuclear_triggered=True,
                wake_up_lock=True,
                data_fault=True,
                data_valid=False,
                liquidation_confirmed=False,
                halt_reason=reason,
                official_overall_floor=levels["official_overall_floor"],
                operating_overall_floor=levels["operating_overall_floor"],
                official_daily_floor=None,
                operating_daily_floor=None,
                baseline_status="unverified",
                monitoring_status="overall_only",
            )
        return self._save_state(force_emergency=True)

    def request_data_fault(self, reason):
        # No HTTP or sleep, and never hold the memory lock during persistence.
        with self._state_lock:
            self.state.update(data_fault=True, data_valid=False, wake_up_lock=True,
                              fault_reason=reason, acknowledged_control=None, macro_resume_pending=False)
            self.state["fault_revision"] += 1
            self._pending_fault = True
        if not publish_fault_event(self.state_file.parent, self.profile, reason):
            log.error("Fault event storage unavailable; memory latch and heartbeat remain active")

    def request_macro_pause(self, paused):
        with self._state_lock:
            self._pending_macro = bool(paused)

    def _drain_control_requests(self):
        with self._state_lock:
            changed = self._pending_fault or self._pending_macro is not None
            if self._pending_macro is not None:
                if self.state["macro_paused"] and not self._pending_macro:
                    self.state["macro_resume_pending"] = not (
                        self.state["data_fault"] or self.state["nuclear_triggered"])
                self.state["macro_paused"] = self._pending_macro
                self._pending_macro = None
            self._pending_fault = False
            self.state["wake_up_lock"] = bool(self.state["data_fault"] or
                self.state["macro_paused"] or self.state["nuclear_triggered"])
        if changed:
            self._save_state()

    def _coordinate_controls(self):
        status = self.api_client.get_prop_control()
        if (not isinstance(status, dict)
                or any(type(status.get(k)) is not bool for k in ("verified", "flat", "pending_entries"))):
            self._pause_on_fault("Journal/control status unavailable")
            return
        ack = status.get("ack")
        with self._state_lock:
            accepted_ack = False
            if (isinstance(ack, dict) and ack.get("epoch") == status.get("epoch")
                    and isinstance(ack.get("flags"), list)
                    and all(f in ("data_fault", "manual_lock") for f in ack["flags"])
                    and 0 <= time.time() * 1000 - ack.get("created_ms", 0) <= 60000
                    and status.get("verified") is True and status.get("pending_entries") is False
                    and not status.get("risk_halt") and self.state["data_valid"]
                    and not self.state["nuclear_triggered"]):
                if "data_fault" in ack["flags"]:
                    self.state["data_fault"] = False
                self.state["acknowledged_control"] = ack["id"]
                accepted_ack = True
            if status.get("data_fault") and not accepted_ack:
                self.state["data_fault"] = True
            if status.get("risk_halt"):
                self.state["nuclear_triggered"] = True
            self.state["wake_up_lock"] = bool(self.state["data_fault"] or
                self.state["macro_paused"] or self.state["nuclear_triggered"])
            paused = self.state["wake_up_lock"]
            # Only the macro-owned pause may automatically restart trading.
            resume = (not paused and self.state.get("macro_resume_pending") is True
                      and status.get("verified") is True and not status.get("data_fault")
                      and not status.get("manual_lock") and not status.get("risk_halt"))
            if status.get("data_fault") or status.get("manual_lock") or status.get("risk_halt"):
                self.state["macro_resume_pending"] = False
        self._save_state()
        if paused:
            self.api_client.stop_buying()
        elif resume:
            if self.api_client.start_trading():
                with self._state_lock:
                    self.state["macro_resume_pending"] = False
                self._save_state()

    def _pause_on_fault(self, reason):
        self.request_data_fault(reason)
        self._save_state()
        log.error("Entries locked: %s", reason)
        self.api_client.report_fault(reason)
        self.api_client.stop_buying()
        # No automatic /start: authenticated acknowledgement and reconciliation required.

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
        if self.emergency is not None:
            confirmed = self.emergency.step()
            with self._state_lock:
                self.state["liquidation_confirmed"] = confirmed
            self._save_state(force_emergency=bool(self.nuclear_triggered))
            return confirmed
        # Serializes simultaneous crash/DD requests without blocking state reads.
        if not self._emergency_lock.acquire(blocking=False):
            return False
        try:
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
            proof = self.api_client.get_prop_control()
            confirmed = bool(paused and exits_ok and remaining == []
                and isinstance(proof, dict) and proof.get("verified") is True
                and proof.get("flat") is True)
            with self._state_lock:
                self.state["liquidation_confirmed"] = confirmed
            self._save_state()
            if not confirmed:
                log.critical("Emergency halt not confirmed; retrying on the next guard cycle")
            return confirmed
        finally:
            self._emergency_lock.release()

    def _daily_unavailable(self, now, *, read_exposure=True):
        """Report an unknown daily floor; never derive a liquidation threshold from a guess."""
        exposure = {"status": "unknown", "checked_at": now.isoformat()}
        # Only an explicit independent adapter method qualifies, never an RPC delegate.
        reader = getattr(type(self.api_client), "get_account_exposure", None)
        if read_exposure and reader is not None:
            try:
                sample = reader(self.api_client)
                if (not isinstance(sample, dict)
                        or sample.get("source") != "direct_account_snapshot"
                        or any(type(sample.get(k)) is not int or sample[k] < 0
                               for k in ("positions_count", "open_orders_count"))):
                    raise ValueError("Unverified exposure")
                exposure = {**sample, "status": "present" if (
                    sample["positions_count"] or sample["open_orders_count"]) else "flat_observed"}
            except Exception:
                pass  # Unknown is never flat; overall evaluation already has priority.
        with self._state_lock:
            self.state.update(daily_monitoring_available=False, overnight_exposure=exposure,
                              overnight_policy_required=True)
            self.state.setdefault("daily_unavailable_since", now.isoformat())
        reason = "Daily floor unverifiable; exposure=" + exposure["status"] + "; overnight policy required"
        log.critical(reason)
        # The journal fault survives restart, including when the main risk file is corrupt.
        self.request_data_fault(reason)
        return reason

    def _check_drawdown(self, now=None):  # noqa: C901 - atomic state update and limits together
        self._drain_control_requests()
        now_supplied = now
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("UTC-aware timestamp required")
        now = now.astimezone(UTC)
        if self.nuclear_triggered:
            self._enforce_halt()
            return True
        if self.state_error:
            # Latch locally first; never spend the direct-read deadline on RPC.
            self.request_data_fault("Risk state requires reconciliation")
            # Distinguish authorization, monitoring, and emergency liquidation authority (F04):
            # entries remain locked, but supervisor continues sampling direct equity to protect overall floor.
            if self.emergency is not None:
                try:
                    remote = self.api_client.get_prop_profile()
                    if remote != self.profile:
                        raise ValueError("Direct identity unavailable")
                    equity = self.api_client.get_equity()
                    if not finite_number(equity):
                        raise ValueError("Direct equity unavailable")
                    with self._state_lock:
                        self.state["monitoring_status"] = "overall_only"
                    levels = floors(self.profile, self.initial_balance)
                    if Decimal(str(equity)) <= Decimal(str(levels["operating_overall_floor"])):
                        reason = "Internal static overall floor reached"
                        log.critical(
                            "Operating overall floor breached during state fault: equity=%s floor=%s",
                            equity, levels["operating_overall_floor"],
                        )
                        self._persist_emergency_halt(now, float(equity), levels, reason)
                        self._daily_unavailable(now, read_exposure=False)
                        self._enforce_halt()
                        return True
                    self._daily_unavailable(datetime.now(UTC) if now_supplied is None else now)
                except Exception:
                    self.state["monitoring_status"] = "unavailable"
                    log.error("Independent risk monitoring unavailable during state fault")
                    self._daily_unavailable(now, read_exposure=False)
            else:
                self.state["monitoring_status"] = "unavailable"
                log.error("Independent risk monitoring unavailable: no emergency adapter")
            if "overnight_exposure" not in self.state:
                self._daily_unavailable(now, read_exposure=False)
            self._pause_on_fault("Risk state requires reconciliation")
            return False
        with self._state_lock:
            fault_revision = self.state["fault_revision"]
        try:
            remote = self.api_client.get_prop_profile()
            if remote != self.profile:
                raise ValueError("Freqtrade/supervisor profile mismatch or unavailable")
            if self.state["last_updated"] and utc_time(self.state["last_updated"]) > now:
                raise ValueError("Clock moved backwards")
        except UnverifiedAccount as exc:
            self.state["monitoring_status"] = "unavailable"
            self._daily_unavailable(now, read_exposure=False)
            self._pause_on_fault(str(exc))
            return False
        except (ValueError, TypeError, KeyError) as exc:
            self.state["monitoring_status"] = "unavailable"
            self._daily_unavailable(now, read_exposure=False)
            self.state_error = str(exc)
            self._pause_on_fault(str(exc))
            return False
        equity = self.api_client.get_equity()
        if not finite_number(equity):
            self.state["monitoring_status"] = "unavailable"
            self._daily_unavailable(now, read_exposure=False)
            self._pause_on_fault("Missing/nonfinite full-account equity")
            return False
        # Preserve real observations separately from historical midnight evidence.
        # These are receipt timestamps, not a retrodated exchange measurement.
        received_at = datetime.now(UTC) if now_supplied is None else now
        cycle_started_at = now
        now = received_at  # Baseline day and comparisons follow receipt, not cycle start.
        observation = {"equity": float(equity), "received_at": received_at.isoformat(),
                       "cycle_started_at": cycle_started_at.isoformat(),
                       "source": "direct_account_equity" if self.emergency is not None else "account_equity_adapter"}
        metadata = getattr(self.api_client, "__dict__", {}).get("equity_observation")
        response_day_mismatch = False
        if isinstance(metadata, dict) and metadata.get("equity") == equity:
            response_ms = metadata.get("response_time_ms")
            if finite_number(response_ms):
                response_at = datetime.fromtimestamp(response_ms / 1000, UTC)
                observation.update(response_at=response_at.isoformat(), source=metadata["source"])
                response_day_mismatch = response_at.date() != now.date()
        previous = self.state.get("equity_observation")
        self.state["equity_observation"] = observation
        # Admission-only provenance; never a historical baseline certificate.
        # Missing metadata stays missing and cannot be replaced by save time.
        identity_reads = getattr(self.api_client, "__dict__", {}).get("identity_reads", [])
        equity_read = (metadata.get("read") if isinstance(metadata, dict)
                       and metadata.get("equity") == equity else None)
        self.state["risk_component_reads"] = (
            list(identity_reads) if isinstance(identity_reads, list) else []
        ) + ([equity_read] if isinstance(equity_read, dict) else [])
        try:
            previous_day = utc_time(previous["received_at"]).date() if previous else now.date()
        except (ValueError, KeyError, TypeError, AttributeError):
            previous, previous_day = None, now.date()
        if previous and previous_day != now.date():
            self.state["baseline_estimate"] = {
                "status": "estimated", "confirmed": False,
                "utc_day": now.date().isoformat(),
                "source": "pre_post_observations_not_midnight_evidence",
                "samples": [previous, observation],
            }
        baseline = self.state["baseline"]
        baseline_error = None
        baseline_status = "verified"
        try:
            if response_day_mismatch:
                raise ValueError("Equity response and receipt belong to different UTC days")
            if baseline is None or utc_time(baseline["captured_at"]).date() != now.date():
                candidate = json.loads(self.baseline_file.read_text(encoding="utf-8"))
                validate_baseline(candidate, self.profile, now)
                baseline = candidate
            else:
                validate_baseline(baseline, self.profile, now)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            baseline_error = "Midnight baseline reconciliation required"
            baseline_status = ("estimated" if self.state.get("baseline_estimate", {}).get("utc_day") == now.date().isoformat() else
                               "missing" if isinstance(exc, FileNotFoundError) else "unverified")
            baseline = None
        # Overall protection remains available even while the daily baseline is unknown.
        levels = floors(self.profile, baseline["equity"] if baseline else self.initial_balance)
        reason = None
        if Decimal(str(equity)) <= Decimal(str(levels["operating_overall_floor"])):
            reason = "Internal static overall floor reached"
        if baseline and Decimal(str(equity)) <= Decimal(str(levels["operating_daily_floor"])):
            reason = "Internal static daily floor reached"
        with self._state_lock:
            # Recomputed every cycle: no sticky latch from a previous process/state.
            remote_verified = bool(
                self.emergency is not None
                or getattr(self.api_client, "__dict__", {}).get("direct") is not None
            )
            self.state.update(last_equity=equity, last_updated=now.isoformat(),
                              baseline=baseline, baseline_status=baseline_status,
                              monitoring_status="overall_only" if baseline_error else "daily_and_overall",
                              current_trading_day=now.date().isoformat(),
                              remote_identity_verified=remote_verified,
                              data_valid=not bool(baseline_error) and self.state["fault_revision"] == fault_revision)
            self.state["data_fault"] = self.state["data_fault"] or bool(baseline_error)
            self.state["wake_up_lock"] = bool(self.state["data_fault"] or self.state["macro_paused"])
            self.state.update(levels)
            if not baseline:
                self.state["official_daily_floor"] = None
                self.state["operating_daily_floor"] = None
            if reason:
                self.state.update(nuclear_triggered=True, wake_up_lock=True,
                                  liquidation_confirmed=False, halt_reason=reason)
        if baseline_error:
            baseline_error = self._daily_unavailable(now, read_exposure=not bool(reason))
        else:
            with self._state_lock:
                self.state.update(daily_monitoring_available=True, overnight_policy_required=False)
                self.state.pop("daily_unavailable_since", None)
        saved = self._save_state()
        if not saved:
            self._pause_on_fault(self.state_error)
        if reason:
            self._enforce_halt()
            return True
        if baseline_error:
            self._pause_on_fault(baseline_error)
        elif saved:
            self._coordinate_controls()
        return False

    def _handle_shutdown(self, signum, frame):
        self._shutdown.set()
        self.request_data_fault("Supervisor shutdown")

    def run(self):
        while not self._shutdown.is_set():
            budget = getattr(self.api_client, "operation_budget", None)
            with budget(4.0) if budget else nullcontext():
                try:
                    self._check_drawdown()
                    expired = getattr(self.api_client, "deadline_expired", lambda: False)
                    if expired():
                        self._pause_on_fault("Risk operation deadline exceeded")
                except Exception as exc:
                    log.exception("Guard cycle failed")
                    try:
                        self._pause_on_fault(type(exc).__name__)
                    except Exception:
                        log.exception("Unable to pause; entry heartbeat will expire")
            self._shutdown.wait(self.interval)
        self.request_data_fault("Supervisor shutdown")
        self._save_state()
