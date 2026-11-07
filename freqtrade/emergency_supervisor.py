"""Journal-backed emergency reductions without importing Freqtrade ORM or RPC."""

import hashlib
import json
import logging
import math
import os
import sqlite3
import time
import uuid
from pathlib import Path

from freqtrade.order_journal import JournalBlocked, OrderJournal
from freqtrade.supervisor_bybit import UnverifiedAccount


logger = logging.getLogger(__name__)


class SupervisorLease:
    def __init__(self, path):
        self.handle = Path(path).open("a+b")
        try:
            if os.fstat(self.handle.fileno()).st_size == 0:
                self.handle.write(b"0")
                self.handle.flush()
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise JournalBlocked("Another supervisor owns this shared volume") from None

    def close(self):
        self.handle.close()


class EmergencySupervisor:
    def __init__(self, adapter, directory, state_file, profile, api_key, *, coordinated=False):
        self.adapter, self.directory = adapter, Path(directory)
        self.state_file, self.profile = Path(state_file), profile
        self.owner, self.coordinated = uuid.uuid4().hex, coordinated
        self.journal = None
        # Existing journal is mandatory. Never bootstrap an empty account history.
        connection = self.connection()
        try:
            row = connection.execute("SELECT value FROM binding WHERE id=1").fetchone()
            binding = json.loads(row[0])
        finally:
            connection.close()
        expected = {k: profile[k] for k in ("account_id", "challenge_id", "phase_id", "profile_id",
                                           "exchange", "environment", "currency")}
        expected["key_fingerprint"] = hashlib.sha256(api_key.encode()).hexdigest()
        if any(binding.get(k) != v for k, v in expected.items()) or binding.get("schema") != 1:
            raise JournalBlocked("Supervisor/journal identity mismatch")
        self.binding = {k: v for k, v in binding.items() if k != "schema"}
        self.lease = SupervisorLease(self.directory / "supervisor.lock")

    def connection(self):
        return sqlite3.connect((self.directory / "orders.sqlite").resolve().as_uri() + "?mode=rw",
                               uri=True, timeout=0.05)

    def close(self):
        if self.journal:
            self.journal.close()
        self.lease.close()

    @property
    def owned_journal(self) -> OrderJournal:
        """Journal acquired by liquidate(); helpers never run without ownership."""
        if self.journal is None:
            raise JournalBlocked("Supervisor does not own the order journal")
        return self.journal

    def record(self, **updates):
        connection = self.connection()
        try:
            with connection:
                row = connection.execute("SELECT value FROM emergency WHERE id=1").fetchone()
                state = json.loads(row[0]) if row else {"halt_id": uuid.uuid4().hex, "handoff": False}
                previous_phase = state.get("phase")
                state.update(updates, updated_ms=int(time.time() * 1000), db_reconciliation_required=True)
                connection.execute("INSERT OR REPLACE INTO emergency VALUES (1,?)", (json.dumps(state),))
                connection.execute("INSERT INTO control_audit VALUES (?,?,?)",
                    (uuid.uuid4().hex, int(time.time() * 1000), json.dumps({"emergency": state})))
                if state.get("phase") != previous_phase:
                    logger.warning("Emergency phase=%s confirmed=%s", state.get("phase"), state.get("confirmed"))
                return state
        finally:
            connection.close()

    def _evidence(self, intent, order):
        request = json.loads(intent["request"])
        expected_amount = self.adapter.expected_amount(request)
        previous = json.loads(intent["evidence"]) if intent["evidence"] else {}
        if (order["clientOrderId"] != intent["client_id"] or order["symbol"] != request["pair"]
                or order["side"] != request["side"] or order["reduceOnly"] is not request["reduceOnly"]
                or (intent["exchange_id"] and intent["exchange_id"] != order["id"])
                or not math.isclose(order["amount"], expected_amount, rel_tol=1e-8, abs_tol=1e-12)
                or (previous.get("filled") or 0) > order["filled"]):
            raise UnverifiedAccount("Journal/exchange origin or quantity mismatch")
        terminal = order["status"] != "open"
        with self.owned_journal.db:
            emergency = self.owned_journal.db.execute("SELECT 1 FROM emergency_orders WHERE client_id=?",
                                                (intent["client_id"],)).fetchone()
            self.owned_journal.db.execute("UPDATE intents SET exchange_id=?,evidence=?,state=?,settled=? WHERE client_id=?",
                (order["id"], json.dumps(order), order["status"],
                 int(terminal) if emergency else intent["settled"], intent["client_id"]))

    def _reconcile(self):
        unresolved = False
        for intent in self.owned_journal.intents():
            request = json.loads(intent["request"])
            order = self.adapter.lookup(request["pair"], client_id=intent["client_id"])
            if order is None:
                unresolved = True
            else:
                self._evidence(intent, order)
        return unresolved

    def _cancel(self, intent, order):
        oid, pair = order["id"], order["symbol"]
        with self.owned_journal.db:
            self.owned_journal.db.execute("INSERT OR REPLACE INTO cancellations VALUES (?,?,?,'requested',?)",
                                   (oid, pair, order["side"], json.dumps(order)))
        try:
            self.adapter.cancel_entry(order)
        except UnverifiedAccount:
            pass
        proof = self.adapter.lookup(pair, client_id=intent["client_id"])
        if proof is None or proof["status"] == "open":
            with self.owned_journal.db:
                self.owned_journal.db.execute("UPDATE cancellations SET state='uncertain' WHERE order_id=?", (oid,))
            return False
        self._evidence(intent, proof)
        with self.owned_journal.db:
            self.owned_journal.db.execute("UPDATE cancellations SET state='confirmed',evidence=? WHERE order_id=?",
                                   (json.dumps(proof), oid))
        return True

    def _owned_positions(self, positions):
        net = {}
        for intent in self.owned_journal.intents(active=False):
            if intent["evidence"]:
                order = json.loads(intent["evidence"])
                filled = order.get("filled") or 0
                pair = json.loads(intent["request"])["pair"]
                net[pair] = net.get(pair, 0) + filled * (1 if order.get("side") == "buy" else -1)
        actual = {p["pair"]: p["amount"] * (1 if p["side"] == "buy" else -1) for p in positions}
        if any(not math.isclose(net.get(pair, 0), actual.get(pair, 0), rel_tol=1e-8, abs_tol=1e-12)
               for pair in set(net) | set(actual)):
            raise UnverifiedAccount("Position provenance/residual differs from journal; manual reconciliation required")

    def step(self):
        with self.adapter.operation_budget(4.0):
            return self._step()

    def _step(self):
        try:
            halt = json.loads(self.state_file.read_text())
            if (halt.get("profile") != self.profile or halt.get("nuclear_triggered") is not True
                    or not halt.get("halt_reason")):
                raise UnverifiedAccount("No persisted safety halt authorizes exchange mutations")
            self.record(reason=halt["halt_reason"], phase="requested", confirmed=False)
            if not self.coordinated:
                self.record(phase="blocked", detail="Single-host dedicated deployment not explicitly attested")
                return False
            self.record(handoff=True, phase="waiting_for_core", confirmed=False,
                        detail="Waiting for exclusive core execution ownership")
            if self.journal is None:
                try:
                    self.journal = OrderJournal(self.directory, self.binding)
                except JournalBlocked:
                    return False  # Never steal a live process's lock, kill it, or use a lease timeout.
            self.record(owner=self.owner, phase="reconciling", detail="Reading direct exchange evidence")
            snapshot = self.adapter.snapshot()
            unresolved = self._reconcile()
            intents = {i["client_id"]: i for i in self.journal.intents(active=False)}
            for order in snapshot["orders"]:
                if order["reduceOnly"]:
                    continue
                intent = intents.get(order.get("clientOrderId"))
                if intent is None:
                    raise UnverifiedAccount("External entry order; no cancellation authority")
                self._evidence(intent, order)
                confirmed = self._cancel(intent, order)
                self.record(phase="reconciling", detail="Entry cancellation confirmed" if confirmed else "Entry cancellation uncertain")
                return False  # Always re-read effective positions in the next step.
            if unresolved:
                raise UnverifiedAccount("Unresolved journal intent; no blind retry or flat confirmation")
            if self.journal.db.execute("SELECT 1 FROM intents i JOIN emergency_orders e USING(client_id) "
                                       "WHERE i.settled=0").fetchone():
                self.record(phase="reconciling", detail="Emergency order remains active")
                return False
            # Re-read after order/fill reconciliation. Snapshot changes are not guessed away.
            snapshot = self.adapter.snapshot()
            if any(not o["reduceOnly"] for o in snapshot["orders"]):
                raise UnverifiedAccount("Entry appeared during reconciliation")
            self._owned_positions(snapshot["positions"])
            if not snapshot["positions"]:
                second = self.adapter.snapshot()
                if second["positions"] or any(not o["reduceOnly"] for o in second["orders"]):
                    raise UnverifiedAccount("Account changed during flat verification")
                self.record(phase="flat_confirmed", confirmed=True, checked_ms=second["checked_ms"],
                            flat_proof={"account_id": self.profile["account_id"],
                                "environment": self.profile["environment"], "positions": [], "entry_orders": [],
                                "remaining_reduce_only_ids": [o["id"] for o in second["orders"]],
                                "first_checked_ms": snapshot["checked_ms"], "second_checked_ms": second["checked_ms"]},
                            detail="Direct exchange proof; local DB reconciliation still mandatory")
                return True
            request = self.adapter.close_request(snapshot["positions"][0])
            self.record(phase="liquidating", owner=self.owner)
            cid = self.journal.prepare_emergency(request, self.owner)
            self.journal.dispatch(cid, self.adapter.create_order)
            return False  # An acknowledgement is not an execution or flat proof.
        except Exception as exc:
            try:
                detail = str(exc) if isinstance(exc, (UnverifiedAccount, JournalBlocked)) else type(exc).__name__
                self.record(phase="blocked", confirmed=False, detail=detail)
            except Exception:
                pass  # Existing durable halt remains. Guard must not report confirmation.
            return False
