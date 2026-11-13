"""Direct, deliberately narrow Bybit safety adapter. Construction performs no I/O."""

import math
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from freqtrade.snapshot_freshness import milliseconds, read_record, validate_reads


class AccountMismatch(ValueError):
    pass


class UnverifiedAccount(Exception):
    pass


def number(value):
    if isinstance(value, bool):
        raise UnverifiedAccount("Boolean quantity")
    result = float(value)
    if not math.isfinite(result):
        raise UnverifiedAccount("Nonfinite quantity")
    return result


class SupervisorBybit:
    def __init__(self, api, profile):
        self.api, self.profile = api, profile
        self.deadline = None
        self.account_type = None
        self.account_status = None

    @contextmanager
    def operation_budget(self, seconds=4.0):
        previous = self.deadline
        until = time.monotonic() + seconds
        self.deadline = min(previous, until) if previous else until
        try:
            yield
        finally:
            self.deadline = previous

    def _request(self, method, *args, raw=True):
        """Invoke a CCXT/Bybit callable within the active operation budget."""
        result = None
        attempts = 3 if raw else 1
        for attempt in range(attempts):
            remaining = (self.deadline - time.monotonic()) if self.deadline else 4.0
            if remaining <= 0:
                raise UnverifiedAccount("Bybit operation deadline expired")
            self.api.timeout = max(1, int(min(2.0, remaining) * 1000))
            try:
                result = method(*args)
                break
            except Exception:
                rem = (self.deadline - time.monotonic()) if self.deadline else 4.0
                if attempt + 1 < attempts and rem > 0.15:
                    continue
                # Never propagate CCXT exception text (may include credentials/request data).
                raise UnverifiedAccount("Bybit request failed") from None
        if self.deadline and time.monotonic() >= self.deadline:
            raise UnverifiedAccount("Late Bybit response")
        return result

    def _call(self, method, *args, raw=True, allow_market_time=False):
        start, monotonic_start = milliseconds(), time.monotonic()
        result = self._request(method, *args, raw=raw)
        if raw:
            if not isinstance(result, dict) or result.get("retCode") != 0:
                raise UnverifiedAccount("Unsuccessful Bybit response")
            record_response = result
            if result.get("time") is None:
                if not allow_market_time:
                    raise UnverifiedAccount("Missing Bybit timestamp")
                # /v5/account/info omits top-level 'time' on live Bybit V5; corroborate clock within budget.
                time_res = self._request(self.api.publicGetV5MarketTime, {}, raw=True)
                if not isinstance(time_res, dict) or time_res.get("retCode") != 0 or time_res.get("time") is None:
                    raise UnverifiedAccount("Missing Bybit market time")
                record_response = {**result, "time": time_res["time"]}
            if not -2000 <= time.time() * 1000 - number(record_response["time"]) <= 15000:
                raise UnverifiedAccount("Stale Bybit response")
            try:
                endpoint = getattr(method, "__name__", None)
                self.last_read = read_record(endpoint if isinstance(endpoint, str) else "response",
                                             start, monotonic_start, record_response)
                if getattr(self, "_snapshot_reads", None) is not None:
                    self._snapshot_reads.append(self.last_read.copy())
            except ValueError:
                raise UnverifiedAccount("Unverified Bybit read interval") from None
        return result

    def verify_identity(self):
        self.identity_reads = []
        if self.profile["environment"] not in ("demo", "live"):
            raise AccountMismatch("Unsupported Bybit environment")
        expected = "https://api-demo.bybit.com" if self.profile["environment"] == "demo" else "https://api.bybit.com"
        if self.api.id != "bybit" or self.api.implode_hostname(self.api.urls["api"]["private"]) != expected:
            raise AccountMismatch("Bybit environment mismatch")
        identity = self._call(self.api.privateGetV5UserQueryApi, {})["result"]
        self.identity_reads.append({**self.last_read, "component": "identity"})
        if str(identity.get("userID", "")) != self.profile["account_id"]:
            raise AccountMismatch("Remote Bybit UID missing or mismatched")
        if type(identity.get("readOnly")) is not int or identity["readOnly"] != 0:
            raise AccountMismatch("Trading authority is unavailable")
        account = self._call(self.api.privateGetV5AccountInfo, {}, allow_market_time=True)["result"]
        self.identity_reads.append({**self.last_read, "component": "account_mode"})
        status = account.get("unifiedMarginStatus")
        if type(status) is not int or status not in (1, 3, 4, 5, 6):
            raise AccountMismatch("Unsupported account mode")
        if status in (5, 6) and account.get("marginMode") != "ISOLATED_MARGIN":
            raise AccountMismatch("Account is not in configured isolated mode")
        self.account_status = status
        self.account_type = "CONTRACT" if status == 1 else "UNIFIED"
        return self.profile

    def equity(self):
        self.equity_observation = None
        if self.account_type is None:
            raise UnverifiedAccount("Account identity must be verified first")
        response = self._call(self.api.privateGetV5AccountWalletBalance, {"accountType": self.account_type})
        accounts = response["result"]["list"]
        if len(accounts) != 1 or accounts[0]["accountType"] != self.account_type:
            raise UnverifiedAccount("Ambiguous wallet account")
        equity = []
        for coin in accounts[0]["coin"]:
            values = [number(coin[k]) for k in ("equity", "walletBalance", "borrowAmount")]
            if coin["coin"] == "USDT":
                equity.append(values[0])
                upl_raw = coin.get("unrealisedPnl", 0)
                upl = number(upl_raw) if upl_raw not in (None, "") else 0.0
                self._last_wallet_detail = {
                    "equity": values[0],
                    "wallet_balance": values[1],
                    "unrealised_pnl": upl,
                }
            elif any(values):
                raise UnverifiedAccount("Non-USDT collateral/exposure cannot be certified")
        if len(equity) != 1:
            raise UnverifiedAccount("USDT equity unavailable")
        self.equity_observation = {"equity": equity[0], "response_time_ms": number(response["time"]),
                                   "source": "bybit_v5_wallet_response",
                                   "read": {**self.last_read, "component": "equity"}}
        return equity[0]

    def _pages(self, endpoint, params):
        rows, seen = [], set()
        request = {"category": "linear", "limit": 50, **params}
        for _ in range(1000):
            result = self._call(endpoint, request.copy()).get("result")
            if (not isinstance(result, dict) or result.get("category") != request["category"]
                    or not isinstance(result.get("list"), list)):
                raise UnverifiedAccount("Incomplete account page")
            rows.extend(result["list"])
            cursor = result["nextPageCursor"]
            if cursor == "":
                return rows
            if not isinstance(cursor, str) or cursor in seen:
                raise UnverifiedAccount("Invalid pagination")
            seen.add(cursor)
            request["cursor"] = cursor
        raise UnverifiedAccount("Incomplete pagination")

    def market(self, symbol):
        if not self.api.markets:
            self._call(self.api.load_markets, raw=False)
        matches = [m for m in self.api.markets.values() if symbol in (m["symbol"], m["id"])
                   and m.get("swap") and m.get("linear") and m.get("settle") == "USDT"]
        if len(matches) != 1 or number(matches[0]["contractSize"]) <= 0:
            raise UnverifiedAccount("Unsupported linear contract")
        return matches[0]

    def _order(self, raw):
        market = self.market(raw["symbol"])
        if type(raw.get("reduceOnly")) is not bool or int(raw["positionIdx"]) != 0:
            raise UnverifiedAccount("Unknown order mode")
        order = self.api.parse_order(raw, market)
        for key in ("amount", "filled", "remaining"):
            order[key] = number(order[key]) * number(market["contractSize"])
        order["reduceOnly"] = raw["reduceOnly"]
        if (not order.get("id") or order.get("side") not in ("buy", "sell")
                or order["amount"] <= 0 or not 0 <= order["filled"] <= order["amount"]
                or not 0 <= order["remaining"] <= order["amount"]
                or order["status"] not in ("open", "closed", "canceled", "cancelled", "expired", "rejected")):
            raise UnverifiedAccount("Incomplete order evidence")
        if order["filled"] and number(order.get("average") or order.get("price")) <= 0:
            raise UnverifiedAccount("Execution price unavailable")
        if order["status"] == "open" and not math.isclose(order["filled"] + order["remaining"], order["amount"], rel_tol=1e-8):
            raise UnverifiedAccount("Inconsistent partial order")
        if order["status"] == "closed" and (order["filled"] <= 0 or order["remaining"] != 0):
            raise UnverifiedAccount("Inconsistent closed order")
        return order

    def snapshot(self):
        self._snapshot_reads = []
        try:
            result = self._snapshot()
            validate_reads(self._snapshot_reads)
            result["reads"] = list(self._snapshot_reads)
            return result
        except ValueError:
            raise UnverifiedAccount("Incoherent account snapshot read interval") from None
        finally:
            self._snapshot_reads = None

    def _snapshot(self):
        self.verify_identity()
        equity = self.equity()
        # A linear-USDT-only claim is not inferred merely from a USDT wallet.
        # Unsupported endpoints are uncertainty, not empty account evidence.
        for params in ({"category": "spot"}, {"category": "inverse"}, {"category": "option"},
                       {"category": "linear", "settleCoin": "USDC"}):
            if self._pages(self.api.privateGetV5OrderRealtime, params):
                raise UnverifiedAccount("Orders outside configured account scope")
            if params["category"] != "spot":
                other = self._pages(self.api.privateGetV5PositionList, params)
                if any(number(p["size"]) != 0 for p in other):
                    raise UnverifiedAccount("Positions outside configured account scope")
        opened = self._pages(self.api.privateGetV5OrderRealtime, {"settleCoin": "USDT", "openOnly": 0})
        positions, _ = self._fetch_usdt_positions()
        orders = [self._order(o) for o in opened]
        if len({o["id"] for o in orders}) != len(orders):
            raise UnverifiedAccount("Duplicate open order evidence")
        return {"equity": equity, "positions": positions, "orders": orders,
                "checked_ms": int(time.time() * 1000)}

    def _fetch_usdt_positions(self):
        positions = []
        pos_details = {}
        for raw in self._pages(self.api.privateGetV5PositionList, {"settleCoin": "USDT"}):
            market = self.market(raw["symbol"])
            size = number(raw["size"])
            if size < 0 or int(raw["positionIdx"]) != 0:
                raise UnverifiedAccount("Unsupported position mode")
            if self.account_status in (1, 3, 4) and size and int(raw["tradeMode"]) != 1:
                raise UnverifiedAccount("Position is not isolated")
            if size:
                if raw["side"] not in ("Buy", "Sell"):
                    raise UnverifiedAccount("Unknown position direction")
                pos_upl = number(raw["unrealisedPnl"]) if raw.get("unrealisedPnl") not in (None, "") else None
                avg_price = number(raw["avgPrice"]) if raw.get("avgPrice") not in (None, "") else None
                pos_details[market["symbol"]] = {
                    "market_id": market["id"],
                    "side": raw["side"].lower(),
                    "contracts": size,
                    "amount": size * number(market["contractSize"]),
                    "mark_price": number(raw["markPrice"]),
                    "unrealised_pnl": pos_upl,
                    "avg_price": avg_price,
                }
                positions.append({"pair": market["symbol"], "side": raw["side"].lower(),
                    "contracts": size, "amount": size * number(market["contractSize"]),
                    "mark_price": number(raw["markPrice"])})
        self._last_pos_details = pos_details
        if len({p["pair"] for p in positions}) != len(positions):
            raise UnverifiedAccount("Multiple positions for one-way pair")
        return positions, pos_details

    def certify_flat_midnight_baseline(self, now_utc=None):
        """A flat wallet does not prove historical equity or absence of transfers.

        No request is made: this adapter has no complete, verified capital-flow
        reconstruction contract. Existing caller must supply reviewed evidence.
        """
        raise UnverifiedAccount("Historical midnight equity cannot be inferred from a flat account")

    def _tx_pages(self, start_ms, end_ms):
        endpoint = (
            self.api.privateGetV5AccountTransactionLog
            if self.account_type == "UNIFIED"
            else self.api.privateGetV5AccountContractTransactionLog
        )
        rows, seen = [], set()
        request = {
            "currency": self.profile["currency"],
            "startTime": int(start_ms),
            "endTime": int(end_ms),
            "limit": 50,
        }
        if self.account_type == "UNIFIED":
            request["accountType"] = "UNIFIED"
        for _ in range(1000):
            result = self._call(endpoint, request.copy()).get("result")
            if not isinstance(result, dict) or not isinstance(result.get("list"), list):
                raise UnverifiedAccount("Incomplete Bybit transaction log page")
            rows.extend(result["list"])
            cursor = result.get("nextPageCursor", "")
            if not cursor:
                return rows
            if not isinstance(cursor, str) or cursor in seen:
                raise UnverifiedAccount("Invalid transaction log pagination")
            seen.add(cursor)
            request["cursor"] = cursor
        raise UnverifiedAccount("Incomplete transaction log pagination")

    def _reconstruct_pair_midnight(self, pair, pos_info, pair_fills, get_closed_pnl):
        """Walk backward from current position across post-midnight fills to 00:00:00 UTC."""
        eps = 1e-8
        if pos_info:
            q = float(pos_info["amount"]) if pos_info["side"] == "buy" else -float(pos_info["amount"])
            avg = pos_info["avg_price"]
        else:
            q = 0.0
            avg = None

        steps = []
        for fill in pair_fills:
            qty = fill["qty"]
            closed_sz = fill["closed_size"]
            direction = 1.0 if fill["side"] == "Buy" else -1.0
            price = fill["price"]
            if 0.0 < closed_sz < qty - eps:
                # Reverse of a flip fill: first undo newly opened tail, then prior close.
                steps.append({
                    "delta": direction * (qty - closed_sz),
                    "price": price,
                    "order_id": fill["order_id"],
                })
                steps.append({
                    "delta": direction * closed_sz,
                    "price": price,
                    "order_id": fill["order_id"],
                })
            else:
                steps.append({
                    "delta": direction * qty,
                    "price": price,
                    "order_id": fill["order_id"],
                })

        for step in steps:
            d = step["delta"]
            p = step["price"]
            if abs(q) <= eps:
                closed_map = get_closed_pnl()
                avg_entry = closed_map.get((pair, step["order_id"])) if step["order_id"] else None
                if avg_entry is None or avg_entry <= 0:
                    raise UnverifiedAccount(f"Missing closed-pnl avgEntryPrice for post-midnight close of {pair}")
                q = -d
                avg = avg_entry
            elif (q > 0) == (d > 0):
                if abs(q) < abs(d) - eps:
                    raise UnverifiedAccount(f"Post-midnight fills for {pair} cross zero without closedSize")
                q_prev = q - d
                if abs(q_prev) <= eps:
                    q = 0.0
                    avg = None
                else:
                    if avg is None or avg <= 0:
                        raise UnverifiedAccount(f"Missing avgPrice for post-midnight add on {pair}")
                    avg_prev = (abs(q) * float(avg) - abs(d) * p) / abs(q_prev)
                    if not math.isfinite(avg_prev) or avg_prev <= 0:
                        raise UnverifiedAccount(f"Cannot bound historical avgPrice for {pair}")
                    q = q_prev
                    avg = avg_prev
            else:
                q = q - d
                if avg is None and pos_info and pos_info.get("avg_price") is not None:
                    avg = pos_info["avg_price"]

        if abs(q) <= eps:
            return None
        return {
            "side": "buy" if q > 0 else "sell",
            "size": abs(q),
            "avg_price": avg,
        }

    def reconcile_midnight_baseline(self, now_utc=None):
        """Reconstruct verified 00:00:00 UTC equity from Bybit V5 ledgers and mark-price klines."""
        allowed_tx_types = {
            "TRADE", "SETTLEMENT", "FUNDING_FEE", "FEE", "FEE_REFUND",
            "REALIZED_PNL", "DELIVERY", "INTEREST", "BORROW", "REPAY",
            "LIQUIDATION", "ADL",
        }
        fill_exec_types = {"Trade", "AdlTrade", "BustTrade", "BlockTrade"}
        non_fill_exec_types = {"Funding", "Settle", "SessionSettlePnL"}
        now_dt = (now_utc or datetime.now(UTC)).astimezone(UTC)
        midnight_dt = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        midnight_ms = int(midnight_dt.timestamp() * 1000)
        now_ms = int(now_dt.timestamp() * 1000)
        if now_ms < midnight_ms:
            raise UnverifiedAccount("Target timestamp precedes UTC midnight")

        snap = wallet_now = pos_details = tx_rows = exec_rows = query_end_ms = None
        for _ in range(2):
            snap = self._snapshot()
            wallet_detail = getattr(self, "_last_wallet_detail", None)
            if not isinstance(wallet_detail, dict):
                raise UnverifiedAccount("USDT wallet balance detail unavailable")
            wallet_now = float(wallet_detail["wallet_balance"])
            pos_details = getattr(self, "_last_pos_details", {})
            query_end_ms = max(now_ms, int(snap["checked_ms"])) + 5_000

            tx_rows = self._tx_pages(midnight_ms - 60_000, query_end_ms)
            exec_rows = self._pages(
                self.api.privateGetV5ExecutionList,
                {"category": "linear", "startTime": midnight_ms - 5_000, "endTime": query_end_ms},
            )
            self.equity()
            wallet_after_detail = getattr(self, "_last_wallet_detail", None)
            if not isinstance(wallet_after_detail, dict):
                raise UnverifiedAccount("USDT wallet balance detail unavailable")
            wallet_after = float(wallet_after_detail["wallet_balance"])
            _, pos_after = self._fetch_usdt_positions()
            before_sig = {p: (d["side"], round(float(d["amount"]), 8)) for p, d in pos_details.items()}
            after_sig = {p: (d["side"], round(float(d["amount"]), 8)) for p, d in pos_after.items()}
            if math.isclose(wallet_now, wallet_after, rel_tol=0.0, abs_tol=1e-9) and before_sig == after_sig:
                break
        else:
            raise UnverifiedAccount("Account mutated during midnight baseline reconciliation")

        post_midnight_delta = 0.0
        for row in tx_rows:
            tx_type = str(row.get("type", "")).upper()
            if tx_type not in allowed_tx_types:
                raise UnverifiedAccount(f"External or unverified capital flow detected: {tx_type or 'UNKNOWN'}")
            tx_time = int(number(row["transactionTime"]))
            change = number(row["change"])
            if tx_time >= midnight_ms:
                post_midnight_delta += change
        wallet_midnight = round(wallet_now - post_midnight_delta, 6)
        if wallet_midnight <= 0:
            raise UnverifiedAccount("Reconstructed midnight wallet balance non-positive")

        post_execs = [e for e in exec_rows if int(number(e["execTime"])) >= midnight_ms]
        fills_by_pair = {}
        for row in post_execs:
            exec_type = row.get("execType")
            if exec_type in non_fill_exec_types:
                continue
            if exec_type not in (None, "") and exec_type not in fill_exec_types:
                raise UnverifiedAccount(f"Unsupported post-midnight execution type: {exec_type}")
            sym_info = self.market(row["symbol"])
            pair = sym_info["symbol"]
            c_size = number(sym_info["contractSize"])
            qty = number(row["execQty"]) * c_size
            if qty <= 0:
                continue
            side = row.get("side")
            if side not in ("Buy", "Sell"):
                raise UnverifiedAccount("Unknown post-midnight execution side")
            exec_price = number(row["execPrice"])
            if exec_price <= 0:
                raise UnverifiedAccount(f"Missing execution price for {pair}")
            closed_raw = row.get("closedSize")
            closed_size = number(closed_raw) * c_size if closed_raw not in (None, "") else 0.0
            fills_by_pair.setdefault(pair, []).append({
                "exec_time": int(number(row["execTime"])),
                "seq": int(number(row["seq"])) if row.get("seq") not in (None, "") else 0,
                "side": side,
                "qty": qty,
                "price": exec_price,
                "closed_size": closed_size,
                "order_id": str(row.get("orderId") or ""),
            })

        closed_pnl_cache = None

        def get_closed_pnl():
            nonlocal closed_pnl_cache
            if closed_pnl_cache is not None:
                return closed_pnl_cache
            rows = self._pages(
                self.api.privateGetV5PositionClosedPnl,
                {"startTime": midnight_ms - 5_000, "endTime": query_end_ms},
            )
            mapping = {}
            for c_row in rows:
                c_pair = self.market(c_row["symbol"])["symbol"]
                c_oid = str(c_row.get("orderId") or "")
                c_avg = number(c_row.get("avgEntryPrice") or 0)
                if c_oid and c_avg > 0:
                    mapping.setdefault((c_pair, c_oid), c_avg)
            closed_pnl_cache = mapping
            return mapping

        positions_midnight = []
        for pair in sorted(set(pos_details.keys()) | set(fills_by_pair.keys())):
            info = pos_details.get(pair)
            pair_fills = fills_by_pair.get(pair, [])
            pair_fills.sort(key=lambda item: (item["exec_time"], item["seq"]), reverse=True)
            mid_state = self._reconstruct_pair_midnight(pair, info, pair_fills, get_closed_pnl)
            if mid_state is None:
                continue
            size_mid = float(mid_state["size"])
            market_id = info["market_id"] if info else self.market(pair)["id"]
            kline_res = self._call(
                self.api.publicGetV5MarketMarkPriceKline,
                {"category": "linear", "symbol": market_id, "interval": "1",
                 "start": midnight_ms, "limit": 1},
            )
            bars = (kline_res.get("result") or {}).get("list") or []
            bar = next((b for b in bars if int(number(b[0])) == midnight_ms), None)
            if bar is None or len(bar) < 2:
                raise UnverifiedAccount(f"Missing 00:00 UTC mark-price candle for {pair}")
            mark_mid = number(bar[1])
            if mark_mid <= 0:
                raise UnverifiedAccount(f"Non-positive 00:00 UTC mark price for {pair}")
            direction = 1.0 if mid_state["side"] == "buy" else -1.0
            if not pair_fills and info and info["unrealised_pnl"] is not None:
                upl_mid = float(info["unrealised_pnl"]) - direction * size_mid * (float(info["mark_price"]) - mark_mid)
            elif mid_state["avg_price"] is not None and float(mid_state["avg_price"]) > 0:
                upl_mid = direction * size_mid * (mark_mid - float(mid_state["avg_price"]))
            else:
                raise UnverifiedAccount(f"Missing UPL basis for {pair}")
            positions_midnight.append({
                "symbol": pair,
                "side": mid_state["side"],
                "size": round(size_mid, 8),
                "mark_price_midnight": round(mark_mid, 8),
                "unrealized_pnl": round(upl_mid, 6),
            })

        upl_total = sum(p["unrealized_pnl"] for p in positions_midnight)
        midnight_equity = round(wallet_midnight + upl_total, 6)
        if not math.isfinite(midnight_equity) or midnight_equity <= 0:
            raise UnverifiedAccount("Reconstructed midnight equity invalid")
        ref = f"bybit-v5-txlog-{self.profile['account_id']}-{midnight_dt.date().isoformat()}"
        return {
            "profile": self.profile,
            "confirmed": True,
            "status": "verified",
            "source": "bybit_v5_reconciled_midnight_equity",
            "captured_at": midnight_dt.isoformat(),
            "verified_at": now_dt.isoformat(),
            "equity": midnight_equity,
            "evidence": {
                "kind": "bybit_v5_midnight_equity_reconstruction",
                "reviewed": False,
                "account_id": self.profile["account_id"],
                "reference": ref,
                "equity": midnight_equity,
                "observed_at": midnight_dt.isoformat(),
                "zero_external_transfers": True,
                "wallet_balance_midnight": wallet_midnight,
                "post_midnight_tx_delta": round(post_midnight_delta, 6),
                "positions_midnight": positions_midnight,
                "tx_count": len(tx_rows),
                "exec_count": sum(len(v) for v in fills_by_pair.values()),
                "snapshot_checked_ms": snap["checked_ms"],
            },
        }

    def lookup(self, pair, *, client_id=None, order_id=None):
        market = self.market(pair)
        params = {"symbol": market["id"], **({"orderLinkId": client_id} if client_id else {"orderId": order_id})}
        raw = self._pages(self.api.privateGetV5OrderRealtime, params) + self._pages(self.api.privateGetV5OrderHistory, params)
        if not raw:
            return None
        orders = [self._order(o) for o in raw]
        first = orders[0]
        keys = ("id", "clientOrderId", "symbol", "side", "amount", "filled", "remaining", "status", "reduceOnly")
        if (any(any(o[k] != first[k] for k in keys) for o in orders)
                or first["symbol"] != pair or (client_id and first["clientOrderId"] != client_id)
                or (order_id and first["id"] != order_id)):
            raise UnverifiedAccount("Conflicting order evidence")
        executions = self._pages(self.api.privateGetV5ExecutionList,
            {"symbol": market["id"], "orderId": first["id"], "execType": "Trade"})
        ids, total = set(), 0.0
        for fill in executions:
            if (fill["execId"] in ids or fill["orderId"] != first["id"]
                    or fill["symbol"] != market["id"] or fill["side"].lower() != first["side"]
                    or (client_id and fill["orderLinkId"] != client_id)):
                raise UnverifiedAccount("Execution origin mismatch")
            ids.add(fill["execId"])
            qty = number(fill["execQty"])
            if qty <= 0:
                raise UnverifiedAccount("Invalid execution quantity")
            total += qty * number(market["contractSize"])
        if not math.isclose(total, first["filled"], rel_tol=1e-8, abs_tol=1e-12):
            raise UnverifiedAccount("Executions do not confirm order fills")
        return first

    def cancel_entry(self, order):
        if order["reduceOnly"] is not False:
            raise UnverifiedAccount("Cannot cancel a protective/reducing order")
        self._call(self.api.cancel_order, order["id"], order["symbol"], raw=False)

    def close_request(self, position):
        market = self.market(position["pair"])
        maximum = market.get("info", {}).get("lotSizeFilter", {}).get("maxMktOrderQty")
        contracts = position["contracts"]
        if maximum:
            contracts = min(contracts, number(maximum))
        contracts = self.precision(position["pair"], contracts)
        minimum = market["limits"]["amount"]["min"]
        if minimum is None or contracts <= 0 or contracts > position["contracts"] or contracts < number(minimum):
            raise UnverifiedAccount("Residual quantity cannot be safely rounded/minimum not met")
        min_cost = market.get("limits", {}).get("cost", {}).get("min")
        amount = contracts * number(market["contractSize"])
        if min_cost and amount * position["mark_price"] < number(min_cost):
            raise UnverifiedAccount("Residual below known minimum notional")
        return {"pair": position["pair"], "side": "sell" if position["side"] == "buy" else "buy",
                "amount": amount, "ordertype": "market", "reduceOnly": True}

    def expected_amount(self, request):
        size = number(self.market(request["pair"])["contractSize"])
        return self.precision(request["pair"], request["amount"] / size) * size

    def precision(self, pair, contracts):
        try:
            return number(self.api.amount_to_precision(pair, contracts))
        except Exception:
            raise UnverifiedAccount("Invalid residual precision/minimum") from None

    def create_order(self, *, pair, side, amount, ordertype, reduceOnly, client_order_id):
        if ordertype != "market" or reduceOnly is not True:
            raise UnverifiedAccount("Supervisor can only send market reduce-only orders")
        market = self.market(pair)
        contracts = self.precision(pair, amount / number(market["contractSize"]))
        return self._call(self.api.create_order, pair, "market", side, contracts, None,
            {"reduceOnly": True, "positionIdx": 0, "clientOrderId": client_order_id}, raw=False)
