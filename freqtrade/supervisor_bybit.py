"""Direct, deliberately narrow Bybit safety adapter. Construction performs no I/O."""

import math
import time
from contextlib import contextmanager


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

    def _call(self, method, *args, raw=True):
        remaining = (self.deadline - time.monotonic()) if self.deadline else 4.0
        if remaining <= 0:
            raise UnverifiedAccount("Bybit operation deadline expired")
        self.api.timeout = max(1, int(min(2.0, remaining) * 1000))
        try:
            result = method(*args)
        except Exception:
            # Never propagate CCXT exception text (may include credentials/request data).
            raise UnverifiedAccount("Bybit request failed") from None
        if self.deadline and time.monotonic() >= self.deadline:
            raise UnverifiedAccount("Late Bybit response")
        if raw:
            if not isinstance(result, dict) or result.get("retCode") != 0:
                raise UnverifiedAccount("Unsuccessful Bybit response")
            if not -2000 <= time.time() * 1000 - number(result["time"]) <= 15000:
                raise UnverifiedAccount("Stale Bybit response")
        return result

    def verify_identity(self):
        if self.profile["environment"] not in ("demo", "live"):
            raise AccountMismatch("Unsupported Bybit environment")
        expected = "https://api-demo.bybit.com" if self.profile["environment"] == "demo" else "https://api.bybit.com"
        if self.api.id != "bybit" or self.api.implode_hostname(self.api.urls["api"]["private"]) != expected:
            raise AccountMismatch("Bybit environment mismatch")
        identity = self._call(self.api.privateGetV5UserQueryApi, {})["result"]
        if str(identity.get("userID", "")) != self.profile["account_id"]:
            raise AccountMismatch("Remote Bybit UID missing or mismatched")
        if type(identity.get("readOnly")) is not int or identity["readOnly"] != 0:
            raise AccountMismatch("Trading authority is unavailable")
        account = self._call(self.api.privateGetV5AccountInfo, {})["result"]
        status = account.get("unifiedMarginStatus")
        if type(status) is not int or status not in (1, 3, 4, 5, 6):
            raise AccountMismatch("Unsupported account mode")
        if status in (5, 6) and account.get("marginMode") != "ISOLATED_MARGIN":
            raise AccountMismatch("Account is not in configured isolated mode")
        self.account_status = status
        self.account_type = "CONTRACT" if status == 1 else "UNIFIED"
        return self.profile

    def equity(self):
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
            elif any(values):
                raise UnverifiedAccount("Non-USDT collateral/exposure cannot be certified")
        if len(equity) != 1:
            raise UnverifiedAccount("USDT equity unavailable")
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
        positions = []
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
                positions.append({"pair": market["symbol"], "side": raw["side"].lower(),
                    "contracts": size, "amount": size * number(market["contractSize"]),
                    "mark_price": number(raw["markPrice"])})
        if len({p["pair"] for p in positions}) != len(positions):
            raise UnverifiedAccount("Multiple positions for one-way pair")
        orders = [self._order(o) for o in opened]
        if len({o["id"] for o in orders}) != len(orders):
            raise UnverifiedAccount("Duplicate open order evidence")
        return {"equity": equity, "positions": positions, "orders": orders,
                "checked_ms": int(time.time() * 1000)}

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
