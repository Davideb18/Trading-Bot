"""Durable at-most-one dispatch per intent. No automatic resubmission or trade adoption."""

import json
import math
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from freqtrade.exceptions import ExchangeError


class JournalBlocked(ExchangeError):
    pass


def quantity(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Invalid exchange quantity")
    return value


def same(a, b):
    return math.isclose(quantity(a), quantity(b), rel_tol=1e-8, abs_tol=1e-12)


class OrderJournal:
    """One owner per shared directory, SQLite FULL commits, independent of trade commits.

    The directory must be shared by every process using this account. Separate hosts
    or volumes are not coordinated. Never delete the lock file while a process runs.
    """

    def __init__(self, directory, identity):
        directory = Path(directory).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        self.mutex = threading.RLock()
        self.ready = False
        self.last_reconciliation = None
        self.risk_snapshot = None
        self.db = None
        self.lock = (directory / "orders.lock").open("a+b")
        try:
            if os.fstat(self.lock.fileno()).st_size == 0:
                self.lock.write(b"0")
                self.lock.flush()
            self.lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            self.lock.close()
            raise JournalBlocked("Another order journal owner is running") from e
        try:
            self.db = sqlite3.connect(directory / "orders.sqlite", check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS binding (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT);
                CREATE TABLE IF NOT EXISTS intents (
                    client_id TEXT PRIMARY KEY, created_ms INTEGER NOT NULL,
                    request TEXT NOT NULL, trade_id INTEGER, state TEXT NOT NULL,
                    exchange_id TEXT, evidence TEXT, linked INTEGER NOT NULL DEFAULT 0,
                    settled INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS findings (
                    key TEXT PRIMARY KEY, pair TEXT, detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS checks (
                    id INTEGER PRIMARY KEY CHECK(id=1), checked_ms INTEGER, detail TEXT);
                CREATE TABLE IF NOT EXISTS control (
                    id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS control_audit (
                    event_id TEXT PRIMARY KEY, created_ms INTEGER, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cancellations (
                    order_id TEXT PRIMARY KEY, pair TEXT, side TEXT, state TEXT, evidence TEXT);
                CREATE TABLE IF NOT EXISTS fault_events (
                    event_id TEXT PRIMARY KEY, created_ms INTEGER, reason TEXT, acknowledged INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS emergency (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS emergency_orders (client_id TEXT PRIMARY KEY, halt_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS risk_reservations (
                    client_id TEXT PRIMARY KEY REFERENCES intents(client_id),
                    state TEXT NOT NULL, detail TEXT NOT NULL);
            """)
            binding = json.dumps({"schema": 1, **identity}, sort_keys=True)
            row = self.db.execute("SELECT value FROM binding WHERE id=1").fetchone()
            if row is not None and row[0] != binding:
                raise JournalBlocked("Journal identity/database binding mismatch")
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO binding VALUES (1, ?)", (binding,))
                # Old unresolved intents consume unknown risk, never zero risk.
                for intent in self.intents():
                    if not json.loads(intent["request"]).get("reduceOnly"):
                        self.db.execute("INSERT OR IGNORE INTO risk_reservations VALUES (?, 'held', ?)",
                                        (intent["client_id"], '{"loss_bound": null}'))
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if not self.lock.closed:
            self.lock.close()  # OS releases the process lock, including on crash.

    def intents(self, active=True):
        with self.mutex:
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM intents" + (" WHERE settled=0" if active else "")
            )]

    def prepare(self, request, trade_id=None):
        with self.mutex:
            pair = request["pair"]
            if request.get("reduceOnly"):
                if any(json.loads(r["request"])["pair"] == pair and not r["linked"]
                       for r in self.intents()):
                    raise JournalBlocked("Unresolved intent on exit pair")
                if self.db.execute("SELECT 1 FROM findings WHERE pair=?", (pair,)).fetchone():
                    raise JournalBlocked("Unverified position/order on exit pair")
            elif not self.ready:
                raise JournalBlocked("Order journal reconciliation required before entry")
            client_id = "ftj" + uuid.uuid4().hex  # 35 characters; Bybit maximum is 36.
            with self.db:
                self.db.execute(
                    "INSERT INTO intents(client_id,created_ms,request,trade_id,state) "
                    "VALUES (?,?,?,?, 'prepared')",
                    (client_id, int(time.time() * 1000), json.dumps(request), trade_id),
                )
                if not request.get("reduceOnly"):
                    self.db.execute("INSERT INTO risk_reservations VALUES (?, 'held', ?)",
                                    (client_id, '{"loss_bound": null}'))
            self.ready = False
            return client_id

    def dispatch(self, client_id, create_order):
        """Only prepared -> sending can send. A retry of this ID never sends again."""
        with self.mutex:
            with self.db:
                changed = self.db.execute(
                    "UPDATE intents SET state='sending' WHERE client_id=? AND state='prepared'",
                    (client_id,),
                ).rowcount
            if changed != 1:
                raise JournalBlocked("Intent already dispatched; reconcile, do not retry")
            row = self.db.execute("SELECT * FROM intents WHERE client_id=?", (client_id,)).fetchone()
            try:
                order = create_order(**json.loads(row["request"]), client_order_id=client_id)
                if (not isinstance(order, dict) or not isinstance(order.get("id"), str)
                        or not order["id"] or order.get("clientOrderId") != client_id):
                    raise JournalBlocked("Unverifiable create_order acknowledgement")
                # Acknowledgement alone is not final execution evidence, even if rejected.
                with self.db:
                    self.db.execute(
                        "UPDATE intents SET state='accepted',exchange_id=?,evidence=? WHERE client_id=?",
                        (order["id"], json.dumps(order), client_id),
                    )
                return order
            except Exception:
                with self.db:
                    self.db.execute("UPDATE intents SET state='uncertain' WHERE client_id=?", (client_id,))
                raise

    def bind(self, order_id, trade_id):
        """Call only after the Freqtrade commit. Reconciliation also repairs this crash gap."""
        with self.mutex, self.db:
            self.db.execute("UPDATE intents SET trade_id=? WHERE exchange_id=?", (trade_id, order_id))

    def prepare_emergency(self, request, owner):
        """Explicit supervisor authority; exchange proof replaces unavailable ORM proof.

        The owner must hold this journal's exclusive lifetime lock. Only market
        reductions are accepted; normal entry permissions are never relaxed.
        """
        with self.mutex:
            row = self.db.execute("SELECT value FROM emergency WHERE id=1").fetchone()
            state = json.loads(row[0]) if row else {}
            if (state.get("owner") != owner or state.get("handoff") is not True
                    or state.get("phase") != "liquidating"
                    or request.get("reduceOnly") is not True or request.get("ordertype") != "market"):
                raise JournalBlocked("No exclusive emergency reduction authority")
            if self.db.execute("SELECT 1 FROM intents i JOIN emergency_orders e USING(client_id) "
                               "WHERE i.settled=0").fetchone():
                raise JournalBlocked("Previous emergency order must be reconciled first")
            cid = "ftj" + uuid.uuid4().hex
            with self.db:
                self.db.execute("INSERT INTO intents(client_id,created_ms,request,state) VALUES (?,?,?,'prepared')",
                                (cid, int(time.time() * 1000), json.dumps(request)))
                self.db.execute("INSERT INTO emergency_orders VALUES (?,?)", (cid, state["halt_id"]))
            self.ready = False
            return cid

    def reconcile(self, snapshot, local_orders, local_positions):
        """Read-only exchange evidence; never fabricate local orders or adopt foreign positions.

        local_orders maps exchange IDs to persisted, non-orphan order dictionaries.
        Positions are positive base quantities keyed by (symbol, long/short).
        External-order findings are latched: disappearance is not proof of ownership.
        """
        with self.mutex:
            self.ready = False
            self.risk_snapshot = None
            # Last cycle's linkage must never authorize an exit after this cycle
            # fails to verify the same pair. Settled history remains immutable.
            with self.db:
                self.db.execute("UPDATE intents SET linked=0 WHERE settled=0")
            errors = []
            try:
                if snapshot.get("complete") is not True:
                    raise ValueError("Incomplete exchange snapshot")
                observations = snapshot["orders"]
                actual = {}
                for p in snapshot["positions"]:
                    qty = quantity(p["amount"])
                    key = (p["symbol"], p["side"])
                    if key[1] not in ("long", "short") or key in actual:
                        raise ValueError("Ambiguous position mode")
                    if qty:
                        actual[key] = qty
                mismatches = set()
                for key in set(actual) | set(local_positions):
                    if not same(actual.get(key, 0), local_positions.get(key, 0)):
                        mismatches.add(key[0])
                        errors.append("position_mismatch:" + key[0])
                with self.db:
                    self.db.execute("DELETE FROM findings WHERE key LIKE 'position:%'")
                    intent_pairs = {json.loads(r["request"])["pair"] for r in self.intents()}
                    for key in actual:
                        if key not in local_positions and key[0] not in intent_pairs:
                            self.db.execute("INSERT OR REPLACE INTO findings VALUES (?,?,?)",
                                ("external_position:" + key[0] + ":" + key[1], key[0],
                                 "Position without local trade or journal origin; manual review required"))
                    for pair in mismatches:
                        self.db.execute("INSERT OR REPLACE INTO findings VALUES (?,?,?)",
                                        ("position:" + pair, pair, "Local/exchange quantities differ"))
                    known = {r["client_id"]: r for r in self.intents(active=False)}
                    open_ids = {o.get("id") for o in snapshot["open_orders"]}
                    for oid, local in local_orders.items():
                        if local["status"] == "open" and oid not in open_ids:
                            errors.append("missing_local_open:" + oid)
                    for order in snapshot["open_orders"]:
                        oid, cid = order.get("id"), order.get("clientOrderId")
                        if not oid or not order.get("symbol"):
                            raise ValueError("Malformed open order")
                        if oid not in local_orders and cid not in known:
                            self.db.execute("INSERT OR REPLACE INTO findings VALUES (?,?,?)",
                                            ("external:" + oid, order["symbol"], "Unknown open order"))
                        elif oid in local_orders:
                            local = local_orders[oid]
                            if (order.get("symbol") != local["symbol"]
                                    or order.get("side") != local["side"]
                                    or not same(order.get("amount"), local["amount"])
                                    or not same(order.get("filled"), local["filled"])
                                    or local["status"] != "open"):
                                errors.append("local_order_mismatch:" + oid)
                    for row in self.intents():
                        cid = row["client_id"]
                        request = json.loads(row["request"])
                        order = observations.get(cid)
                        if order is None:
                            errors.append("unresolved:" + cid)
                            continue
                        oid = order.get("id")
                        if (not oid or order.get("clientOrderId") != cid
                                or (row["exchange_id"] and row["exchange_id"] != oid)
                                or order.get("symbol") != request["pair"]
                                or order.get("side") != request["side"]):
                            raise ValueError("Order origin/identity mismatch")
                        amount, filled, remaining = (quantity(order[k]) for k in ("amount", "filled", "remaining"))
                        status = order["status"]
                        if (amount <= 0 or filled > amount or remaining > amount or status not in
                                ("open", "closed", "canceled", "cancelled", "expired", "rejected")):
                            raise ValueError("Invalid order evidence")
                        if status == "open" and not same(amount, filled + remaining):
                            raise ValueError("Invalid partial fill")
                        if status == "closed" and (not filled or remaining != 0):
                            raise ValueError("Invalid closed order")
                        if filled and quantity(order.get("average") or order.get("price")) <= 0:
                            raise ValueError("Missing execution price")
                        # Adapter requires complete execution pagination for this order.
                        if not same(snapshot["fills"][cid], filled):
                            raise ValueError("Executions/order fill mismatch")
                        previous = json.loads(row["evidence"]) if row["evidence"] else {}
                        if quantity(previous.get("filled") or 0) > filled:
                            raise ValueError("Regressing fill evidence")
                        local = local_orders.get(oid)
                        linked = bool(local and local["symbol"] == request["pair"]
                                      and local["side"] == request["side"]
                                      and same(local["filled"], filled)
                                      and same(local["amount"], amount)
                                      and local["status"] == status
                                      and (row["trade_id"] is None or row["trade_id"] == local["trade_id"]))
                        terminal = status != "open"
                        empty = terminal and filled == 0
                        settled = terminal and (linked or empty) and request["pair"] not in mismatches
                        if not (linked or settled) or request["pair"] in mismatches:
                            errors.append("unbound_or_mismatched:" + cid)
                        state = ("filled" if status == "closed" else
                                 "partially_filled" if status == "open" and filled else
                                 "accepted" if status == "open" else status)
                        self.db.execute(
                            "UPDATE intents SET state=?,exchange_id=?,evidence=?,linked=?,settled=?,"
                            "trade_id=COALESCE(trade_id,?) WHERE client_id=?",
                            (state, oid, json.dumps(order), int(linked), int(settled),
                             local["trade_id"] if linked else None, cid),
                        )
                errors += [r[0] for r in self.db.execute("SELECT key FROM findings")]
            except (KeyError, TypeError, ValueError) as e:
                errors.append("inconclusive:" + str(e))
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO checks VALUES (1,?,?)",
                                (int(time.time() * 1000), json.dumps(errors)))
            self.ready = not errors
            if not errors:
                self.risk_snapshot = json.loads(json.dumps(snapshot))
                # Only complete exchange/ORM reconciliation transfers a reservation
                # into actual position exposure or proves an empty cancellation.
                with self.db:
                    self.db.execute("UPDATE risk_reservations SET state='reconciled' "
                                    "WHERE state='held' AND client_id IN "
                                    "(SELECT client_id FROM intents WHERE settled=1)")
            self.last_reconciliation = {
                "checked_ms": int(time.time() * 1000), "verified": not errors,
                "flat": not errors and not snapshot["positions"] and not snapshot["open_orders"],
            }
            return errors
