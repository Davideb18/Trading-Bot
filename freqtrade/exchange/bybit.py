import logging
import math
from datetime import datetime, timedelta

import ccxt

from freqtrade.constants import BuySell
from freqtrade.enums import OPTIMIZE_MODES, MarginMode, PriceType, TradingMode
from freqtrade.exceptions import DDosProtection, ExchangeError, OperationalException, TemporaryError
from freqtrade.exchange import Exchange
from freqtrade.exchange.common import retrier
from freqtrade.exchange.exchange_types import CcxtOrder, FtHas
from freqtrade.misc import deep_merge_dicts
from freqtrade.order_recovery import normalized_order_quantities
from freqtrade.util import dt_from_ts, dt_ts


logger = logging.getLogger(__name__)


class Bybit(Exchange):
    """Bybit exchange class.
    Contains adjustments needed for Freqtrade to work with this exchange.
    """

    unified_account = False

    def _journal_pages(self, endpoint, params, reads=None, component="page"):
        """Read every V5 page, or fail closed; never rely on a truncated CCXT list."""
        import time
        from freqtrade.snapshot_freshness import milliseconds, read_record
        rows, cursors = [], set()
        request = {"category": "linear", "limit": 50, **params}
        for _ in range(1000):
            start, monotonic_start = milliseconds(), time.monotonic()
            response = endpoint(request.copy())
            record = read_record(component, start, monotonic_start, response)
            if reads is not None:
                reads.append(record)
            if response.get("retCode") != 0:
                raise ValueError("Unsuccessful reconciliation response")
            result = response["result"]
            if not isinstance(result["list"], list) or result.get("category") != "linear":
                raise ValueError("Invalid reconciliation page")
            rows.extend(result["list"])
            cursor = result["nextPageCursor"]
            if cursor == "":
                return rows
            if not isinstance(cursor, str) or cursor in cursors:
                raise ValueError("Invalid reconciliation cursor")
            cursors.add(cursor)
            request["cursor"] = cursor
        raise ValueError("Reconciliation pagination limit reached")

    def _journal_market(self, raw):
        matches = [m for m in self._api.markets.values()
                   if m["id"] == raw["symbol"] and m.get("swap") and m.get("linear")
                   and m.get("settle") == "USDT"]
        if len(matches) != 1:
            raise ValueError("Unsupported journal market")
        return matches[0]

    def _order_contracts_to_amount(self, order: CcxtOrder) -> CcxtOrder:
        if order.get("status") == "rejected" and order.get("filled") is None:
            raw = order.get("info") if isinstance(order.get("info"), dict) else None
            _, filled, remaining = normalized_order_quantities(order, raw=raw)
            if filled is not None:
                order["filled"] = filled
            if remaining is not None:
                order["remaining"] = remaining
        return super()._order_contracts_to_amount(order)

    def fetch_journal_snapshot(self, intents):
        """Dedicated USDT linear account only. No writes, cancellations or ownership guesses.

        Query each outstanding client ID in realtime AND history; validate fills
        against the execution endpoint. Missing history remains unresolved.
        """
        import json

        from freqtrade.order_recovery import normalized_order_quantities
        from freqtrade.snapshot_freshness import validate_snapshot
        reads = []

        def parse(raw):
            parsed = self._api.parse_order(raw, self._journal_market(raw))
            _, filled, remaining = normalized_order_quantities(parsed, raw=raw)
            if parsed.get("filled") is None and filled is not None:
                parsed["filled"] = filled
            if parsed.get("remaining") is None and remaining is not None:
                parsed["remaining"] = remaining
            return self._order_contracts_to_amount(parsed)

        opened = self._journal_pages(self._api.privateGetV5OrderRealtime,
                                     {"settleCoin": "USDT", "openOnly": 0}, reads, "open_orders")
        positions = self._journal_pages(self._api.privateGetV5PositionList,
                                        {"settleCoin": "USDT"}, reads, "positions")
        snapshot = {"complete": True, "open_orders": [parse(o) for o in opened],
                    "positions": [], "orders": {}, "fills": {}, "reads": reads}
        for raw in positions:
            market = self._journal_market(raw)
            if int(raw["positionIdx"]) != 0:
                raise ValueError("Journal requires one-way position mode")
            position = self._api.parse_position(raw, market)
            amount = self._contracts_to_amount(market["symbol"], position["contracts"])
            if amount:
                pos_entry = {"symbol": market["symbol"],
                             "side": position["side"], "amount": amount}
                raw_mark = raw.get("markPrice")
                if raw_mark not in (None, "", "0"):
                    pos_entry["mark_price"] = float(raw_mark)
                    pos_read = next((r for r in reversed(reads) if r.get("component") == "positions"), None)
                    if pos_read and pos_read.get("received_ms"):
                        pos_entry["mark_price_observed_ms"] = pos_read["received_ms"]
                snapshot["positions"].append(pos_entry)
        for intent in intents:
            cid = intent["client_id"]
            request = json.loads(intent["request"])
            market = self._api.market(request["pair"])
            params = {"symbol": market["id"], "orderLinkId": cid}
            realtime = self._journal_pages(self._api.privateGetV5OrderRealtime, params, reads, "realtime:" + cid)
            history = self._journal_pages(self._api.privateGetV5OrderHistory, params, reads, "history:" + cid)
            candidates = realtime + history
            if not candidates:
                continue  # Not-found is never evidence of non-execution.
            if any(o.get("orderLinkId") != cid for o in candidates):
                raise ValueError("Exchange did not honor client ID filter")
            if len({o["orderId"] for o in candidates}) != 1:
                raise ValueError("Client ID identifies multiple orders")
            # Refuse inconsistent reads; the next cycle can obtain stable evidence.
            parsed = [parse(o) for o in candidates]
            fields = ("id", "status", "amount", "filled", "remaining", "side", "symbol")
            if any(any(o[k] != parsed[0][k] for k in fields) for o in parsed):
                raise ValueError("Realtime/history disagree")
            raw, order = candidates[0], parsed[0]
            if (raw.get("reduceOnly") is not request["reduceOnly"]
                    or order["symbol"] != request["pair"]
                    or order["side"] != request["side"]
                    or not math.isclose(order["amount"], self.amount_to_contract_precision(
                        request["pair"], request["amount"]), rel_tol=1e-8, abs_tol=1e-12)):
                raise ValueError("Order differs from journal request")
            executions = self._journal_pages(self._api.privateGetV5ExecutionList,
                {"symbol": market["id"], "orderId": order["id"], "execType": "Trade"}, reads, "executions:" + cid)
            ids, total = set(), 0.0
            for execution in executions:
                if (execution["orderId"] != order["id"]
                        or execution.get("orderLinkId") != cid
                        or execution["symbol"] != market["id"]
                        or execution["side"].lower() != request["side"]
                        or execution["execId"] in ids):
                    raise ValueError("Invalid execution identity")
                ids.add(execution["execId"])
                qty = float(execution["execQty"])
                if not math.isfinite(qty) or qty <= 0:
                    raise ValueError("Invalid execution quantity")
                total += self._contracts_to_amount(request["pair"], qty)
            snapshot["orders"][cid] = order
            snapshot["fills"][cid] = total
        # Bracket history/execution pagination with a second account read.
        # Compare quantities/order state, not changing mark prices. This detects
        # observed fills during acquisition; it is not an atomic exchange snapshot.
        final_opened = self._journal_pages(self._api.privateGetV5OrderRealtime,
            {"settleCoin": "USDT", "openOnly": 0}, reads, "open_orders_end")
        final_positions = self._journal_pages(self._api.privateGetV5PositionList,
            {"settleCoin": "USDT"}, reads, "positions_end")

        def signature(rows, fields):
            return sorted(json.dumps({k: row.get(k) for k in fields}, sort_keys=True) for row in rows)

        order_fields = ("orderId", "orderLinkId", "symbol", "side", "qty", "cumExecQty",
                        "leavesQty", "orderStatus", "price", "reduceOnly", "positionIdx")
        position_fields = ("symbol", "side", "size", "positionIdx", "tradeMode")
        if (signature(opened, order_fields) != signature(final_opened, order_fields)
                or signature(positions, position_fields) != signature(final_positions, position_fields)):
            raise ValueError("Account changed during reconciliation reads")
        validate_snapshot(snapshot)
        return snapshot

    def _parse_account_equity(self, response: dict | None) -> dict | None:
        """Preserve coin equity before CCXT's raw wallet response is discarded.

        This strict adapter supports a dedicated USDT-only derivatives account.
        Account-wide USD fields are not applicable to isolated margin. Pending
        order margins and unrealized PnL are already reflected in coin equity.
        """
        if self.trading_mode != TradingMode.FUTURES or self._config["stake_currency"] != "USDT":
            return None
        if not isinstance(response, dict):
            return None
        try:
            if response["retCode"] != 0:
                return None
            accounts = response["result"]["list"]
            if len(accounts) != 1 or accounts[0]["accountType"] not in ("UNIFIED", "CONTRACT"):
                return None
            found = []
            for coin in accounts[0]["coin"]:
                equity = float(coin["equity"])
                wallet = float(coin["walletBalance"])
                debt = float(coin.get("borrowAmount", "0"))
                if not all(math.isfinite(v) for v in (equity, wallet, debt)):
                    return None
                if coin["coin"] == "USDT":
                    found.append(equity)
                elif any(v != 0 for v in (equity, wallet, debt)):
                    return None  # Cannot certify full-account equity in another currency.
            timestamp = response["time"]
            if (
                len(found) != 1
                or isinstance(timestamp, bool)
                or not isinstance(timestamp, (int, float))
                or not math.isfinite(timestamp)
            ):
                return None
            return {
                "value": found[0],
                "currency": "USDT",
                "timestamp": int(timestamp),
                "source": "bybit_wallet_coin_equity",
            }
        except (KeyError, ValueError, TypeError, OverflowError):
            return None

    _ft_has: FtHas = {
        "ohlcv_has_history": True,
        "order_time_in_force": ["GTC", "FOK", "IOC", "PO"],
        "ws_enabled": True,
        "trades_has_history": False,  # Endpoint doesn't support pagination
        "fetch_orders_limit_minutes": 7 * 1440,  # 7 days
        "exchange_has_overrides": {
            # Bybit spot does not support fetch_order
            # Unless the account is unified.
            # TODO: Can be removed once bybit fully forces all accounts to unified mode.
            "fetchOrder": False,
        },
        # Demo trading
        # https://learn.bybit.com/en/bybit-guide/how-to-use-bybit-demo-trading
        "supports_demo_trading": True,
    }
    _ft_has_futures: FtHas = {
        "ohlcv_has_history": True,
        "funding_fee_candle_limit": 200,
        "stoploss_on_exchange": True,
        "stoploss_order_types": {"limit": "limit", "market": "market"},
        "stoploss_blocks_assets": False,
        # bybit response parsing fails to populate stopLossPrice
        "stop_price_prop": "stopPrice",
        "stop_price_type_field": "triggerBy",
        "stop_price_type_value_mapping": {
            PriceType.LAST: "LastPrice",
            PriceType.MARK: "MarkPrice",
            PriceType.INDEX: "IndexPrice",
        },
        "exchange_has_overrides": {
            "fetchOrder": True,
        },
        "has_delisting": True,
    }

    _supported_trading_mode_margin_pairs: list[tuple[TradingMode, MarginMode]] = [
        (TradingMode.SPOT, MarginMode.NONE),
        (TradingMode.FUTURES, MarginMode.ISOLATED),
        # (TradingMode.FUTURES, MarginMode.CROSS),
    ]

    @property
    def _ccxt_config(self) -> dict:
        # Parameters to add directly to ccxt sync/async initialization.
        # ccxt defaults to swap mode.
        config = {}
        if self.trading_mode == TradingMode.SPOT:
            config.update({"options": {"defaultType": "spot"}})
        elif self.trading_mode == TradingMode.FUTURES:
            config.update({"options": {"defaultSettle": self._config["stake_currency"]}})
        config = deep_merge_dicts(config, super()._ccxt_config)
        return config

    @retrier
    def additional_exchange_init(self) -> None:
        """
        Additional exchange initialization logic.
        .api will be available at this point.
        Must be overridden in child methods if required.
        """
        try:
            if not self._config["dry_run"]:
                if self.trading_mode == TradingMode.FUTURES:
                    position_mode = self._api.set_position_mode(False)
                    self._log_exchange_response("set_position_mode", position_mode)
                is_unified = self._api.is_unified_enabled()
                # Returns a tuple of bools, first for margin, second for Account
                if is_unified and len(is_unified) > 1 and is_unified[1]:
                    self.unified_account = True
                    logger.info(
                        "Bybit: Unified account. Assuming dedicated subaccount for this bot."
                    )
                else:
                    self.unified_account = False
                    logger.info("Bybit: Standard account.")
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
            raise TemporaryError(
                f"Error in additional_exchange_init due to {e.__class__.__name__}. Message: {e}"
            ) from e
        except ccxt.BaseError as e:
            raise OperationalException(e) from e

    def _lev_prep(self, pair: str, leverage: float, side: BuySell, accept_fail: bool = False):
        if self.trading_mode != TradingMode.SPOT:
            params = {"leverage": leverage}
            self.set_margin_mode(pair, self.margin_mode, accept_fail=True, params=params)
            self._set_leverage(leverage, pair, accept_fail=True)

    def _get_params(
        self,
        side: BuySell,
        ordertype: str,
        leverage: float,
        reduceOnly: bool,
        time_in_force: str = "GTC",
    ) -> dict:
        params = super()._get_params(
            side=side,
            ordertype=ordertype,
            leverage=leverage,
            reduceOnly=reduceOnly,
            time_in_force=time_in_force,
        )
        if self.trading_mode == TradingMode.FUTURES and self.margin_mode:
            params["position_idx"] = 0
        return params

    def _get_stop_params(self, side: BuySell, ordertype: str, stop_price: float) -> dict:
        params = super()._get_stop_params(
            side=side,
            ordertype=ordertype,
            stop_price=stop_price,
        )
        # work around ccxt bug introduced in https://github.com/ccxt/ccxt/pull/25887
        # Where create_order ain't returning an ID any longer.
        params.update(
            {
                "method": "privatePostV5OrderCreate",
            }
        )
        return params

    def _order_needs_price(self, side: BuySell, ordertype: str) -> bool:
        # Bybit requires price for market orders - but only for classic accounts,
        # and only in spot mode
        return (
            ordertype != "market"
            or (
                side == "buy" and not self.unified_account and self.trading_mode == TradingMode.SPOT
            )
            or self._ft_has.get("marketOrderRequiresPrice", False)
        )

    def dry_run_liquidation_price(
        self,
        pair: str,
        open_rate: float,  # Entry price of position
        is_short: bool,
        amount: float,
        stake_amount: float,
        leverage: float,
        wallet_balance: float,  # Or margin balance
        open_trades: list,
    ) -> float | None:
        """
        Important: Must be fetching data from cached values as this is used by backtesting!
        PERPETUAL:
         bybit:
          https://www.bybithelp.com/HelpCenterKnowledge/bybitHC_Article?language=en_US&id=000001067
          USDT:
            https://www.bybit.com/en/help-center/article/Liquidation-Price-Calculation-under-Isolated-Mode-Unified-Trading-Account#b
          USDC:
            https://www.bybit.com/en/help-center/article/Liquidation-Price-Calculation-under-Isolated-Mode-Unified-Trading-Account#c

        Long USDT:
            Liquidation Price = (
                Entry Price - [(Initial Margin - Maintenance Margin)/Contract Quantity]
                - (Extra Margin Added/Contract Quantity))
        Short USDT:
            Liquidation Price = (
                Entry Price + [(Initial Margin - Maintenance Margin)/Contract Quantity]
                + (Extra Margin Added/Contract Quantity))

        Long USDC:
        Liquidation Price = (
            Position Entry Price - [
                (Initial Margin + Extra Margin Added - Maintenance Margin) / Position Size
            ]
        )

        Short USDC:
        Liquidation Price = (
            Position Entry Price + [
                (Initial Margin + Extra Margin Added - Maintenance Margin) / Position Size
            ]
        )

        Implementation Note: Extra margin is currently not used.
        Due to this - the liquidation formula between USDT and USDC is the same.

        :param pair: Pair to calculate liquidation price for
        :param open_rate: Entry price of position
        :param is_short: True if the trade is a short, false otherwise
        :param amount: Absolute value of position size incl. leverage (in base currency)
        :param stake_amount: Stake amount - Collateral in settle currency.
        :param leverage: Leverage used for this position.
        :param wallet_balance: Amount of margin_mode in the wallet being used to trade
            Cross-Margin Mode: crossWalletBalance
            Isolated-Margin Mode: isolatedWalletBalance
        :param open_trades: List of other open trades in the same wallet
        """

        market = self.markets[pair]
        mm_ratio, _ = self.get_maintenance_ratio_and_amt(pair, stake_amount)

        if self.trading_mode == TradingMode.FUTURES and self.margin_mode == MarginMode.ISOLATED:
            if market["inverse"]:
                raise OperationalException("Freqtrade does not yet support inverse contracts")
            position_value = amount * open_rate
            initial_margin = position_value / leverage
            maintenance_margin = position_value * mm_ratio
            margin_diff_per_contract = (initial_margin - maintenance_margin) / amount

            # See docstring - ignores extra margin!
            if is_short:
                return open_rate + margin_diff_per_contract
            else:
                return open_rate - margin_diff_per_contract

        else:
            raise OperationalException(
                "Freqtrade only supports isolated futures for leverage trading"
            )

    def get_funding_fees(
        self, pair: str, amount: float, is_short: bool, open_date: datetime
    ) -> float:
        """
        Fetch funding fees, either from the exchange (live) or calculates them
        based on funding rate/mark price history
        :param pair: The quote/base pair of the trade
        :param is_short: trade direction
        :param amount: Trade amount
        :param open_date: Open date of the trade
        :return: funding fee since open_date
        :raises: ExchangeError if something goes wrong.
        """
        # Bybit does not provide "applied" funding fees per position.
        if self.trading_mode == TradingMode.FUTURES:
            try:
                return self._fetch_and_calculate_funding_fees(pair, amount, is_short, open_date)
            except ExchangeError:
                logger.warning(f"Could not update funding fees for {pair}.")
        return 0.0

    def fetch_order(self, order_id: str, pair: str, params: dict | None = None) -> CcxtOrder:
        if self.exchange_has("fetchOrder"):
            # Set acknowledged to True to avoid ccxt exception
            params = {"acknowledged": True}

        order = super().fetch_order(order_id, pair, params)
        if not order:
            order = self.fetch_order_emulated(order_id, pair, {})
        if (
            order.get("status") == "canceled"
            and order.get("filled") == 0.0
            and order.get("remaining") == 0.0
        ):
            # Canceled orders will have "remaining=0" on bybit.
            order["remaining"] = None
        return order

    @retrier
    def get_leverage_tiers(self) -> dict[str, list[dict]]:
        """
        Cache leverage tiers for 1 day, since they are not expected to change often, and
        bybit requires pagination to fetch all tiers.
        """

        # Load cached tiers
        tiers_cached = self.load_cached_leverage_tiers(
            self._config["stake_currency"], timedelta(days=1)
        )
        if tiers_cached:
            return tiers_cached

        # Fetch tiers from exchange
        tiers = super().get_leverage_tiers()

        self.cache_leverage_tiers(tiers, self._config["stake_currency"])
        return tiers

    def check_delisting_time(self, pair: str) -> datetime | None:
        """
        Check if the pair gonna be delisted.
        By default, it returns None.
        :param pair: Market symbol
        :return: Datetime if the pair gonna be delisted, None otherwise
        """
        if self._config["runmode"] in OPTIMIZE_MODES:
            return None

        if self.trading_mode == TradingMode.FUTURES:
            return self._check_delisting_futures(pair)
        return None

    def _check_delisting_futures(self, pair: str) -> datetime | None:
        delivery_time = self.markets.get(pair, {}).get("info", {}).get("deliveryTime", 0)
        if delivery_time:
            if isinstance(delivery_time, str) and (delivery_time != ""):
                delivery_time = int(delivery_time)

            if not isinstance(delivery_time, int) or delivery_time <= 0:
                return None

            max_delivery = dt_ts() + (
                14 * 24 * 60 * 60 * 1000
            )  # Assume exchange don't announce delisting more than 14 days in advance

            if delivery_time < max_delivery:
                return dt_from_ts(delivery_time)

        return None


class BybitEU(Bybit):
    """Bybit EU exchange class.
    Minimal adjustment to disable futures trading for the EU version of Bybit.
    """

    _supported_trading_mode_margin_pairs: list[tuple[TradingMode, MarginMode]] = [
        (TradingMode.SPOT, MarginMode.NONE),
    ]
