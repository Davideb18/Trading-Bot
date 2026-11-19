"""
Market Cap PairList provider

Provides dynamic pair list based on Market Cap
"""

import json
import logging
import math
import time
from pathlib import Path

from freqtrade.constants import PairPrefixes
from freqtrade.exceptions import OperationalException
from freqtrade.exchange.exchange_types import Tickers
from freqtrade.plugins.pairlist.IPairList import IPairList, PairlistParameter, SupportsBacktesting
from freqtrade.util import FtTTLCache
from freqtrade.util.coin_gecko import FtCoinGeckoApi


logger = logging.getLogger(__name__)


class MarketCapPairList(IPairList):
    is_pairlist_generator = True
    supports_backtesting = SupportsBacktesting.BIASED

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self._mode = self._pairlistconfig.get("mode", "whitelist")

        if (self._mode == "whitelist") and ("number_assets" not in self._pairlistconfig):
            raise OperationalException(
                "`number_assets` not specified. Please check your configuration "
                'for "pairlist.config.number_assets"'
            )

        self._stake_currency = self._config["stake_currency"]
        self._number_assets = self._pairlistconfig.get("number_assets", 30)
        self._max_rank = self._pairlistconfig.get("max_rank", 30)
        self._refresh_period = self._pairlistconfig.get("refresh_period", 86400)
        self._categories = self._pairlistconfig.get("categories", [])
        self._min_market_cap = float(self._pairlistconfig.get("min_market_cap", 0))
        self._fallback_max_age = float(self._pairlistconfig.get("fallback_max_age", 86400))
        self._preserve_order = bool(self._pairlistconfig.get("preserve_order", False))
        cache_file = self._pairlistconfig.get("cache_file")
        self._cache_file = Path(cache_file) if cache_file else None
        self._last_good_data = None
        self._last_good_at = 0.0
        self._marketcap_cache: FtTTLCache = FtTTLCache(maxsize=2, ttl=self._refresh_period)

        _coingecko_config = self._config.get("coingecko", {})

        self._coingecko: FtCoinGeckoApi = FtCoinGeckoApi(
            api_key=_coingecko_config.get("api_key", ""),
            is_demo=_coingecko_config.get("is_demo", True),
        )

        if self._categories:
            categories = self._coingecko.get_coins_categories_list()
            category_ids = [cat["category_id"] for cat in categories]

            for category in self._categories:
                if category not in category_ids:
                    raise OperationalException(
                        f"Category {category} not in coingecko category list. "
                        f"You can choose from {category_ids}"
                    )

        if self._max_rank > 250:
            self.logger.warning(
                f"The max rank you have set ({self._max_rank}) is quite high. "
                "This may lead to coingecko API rate limit issues. "
                "Please ensure this value is necessary for your use case.",
            )

    def short_desc(self) -> str:
        """
        Short whitelist method description - used for startup-messages
        """
        num = self._number_assets
        rank = self._max_rank
        mode = self._mode
        pair_text = num if (mode == "whitelist") else "blacklisting"
        msg = f"{self.name} - {pair_text} pairs placed within top {rank} market cap."
        return msg

    @staticmethod
    def description() -> str:
        return "Provides pair list based on CoinGecko's market cap rank."

    @staticmethod
    def available_parameters() -> dict[str, PairlistParameter]:
        return {
            "number_assets": {
                "type": "number",
                "default": 30,
                "description": "Number of assets",
                "help": "Number of assets to use from the pairlist",
            },
            "max_rank": {
                "type": "number",
                "default": 30,
                "description": "Max rank of assets",
                "help": "Maximum rank of assets to use from the pairlist",
            },
            "categories": {
                "type": "list",
                "default": [],
                "description": "Coin Categories",
                "help": (
                    "The Category of the coin e.g layer-1 default [] "
                    "(https://www.coingecko.com/en/categories)"
                ),
            },
            "refresh_period": {
                "type": "number",
                "default": 86400,
                "description": "Refresh period",
                "help": "Refresh period in seconds",
            },
            "mode": {
                "type": "option",
                "default": "whitelist",
                "options": ["whitelist", "blacklist"],
                "description": "Mode of operation",
                "help": "Mode of operation (whitelist/blacklist)",
            },
        }

    def get_markets_exchange(self):
        markets = [
            k
            for k in self._exchange.get_markets(
                quote_currencies=[self._stake_currency], tradable_only=True, active_only=True
            ).keys()
        ]

        return markets

    def gen_pairlist(self, tickers: Tickers) -> list[str]:
        """
        Generate the pairlist
        :param tickers: Tickers (from exchange.get_tickers). May be cached.
        :return: List of pairs
        """
        # Generate dynamic whitelist
        # Must always run if this pairlist is the first in the list.
        pairlist = self._marketcap_cache.get("pairlist_mc")
        if pairlist:
            # Item found - no refresh necessary
            return pairlist.copy()
        else:
            # Use fresh pairlist
            # Check if pair quote currency equals to the stake currency.
            _pairlist = self.get_markets_exchange()

            # No point in testing for blacklisted pairs...
            _pairlist = self.verify_blacklist(_pairlist, logger.info)

            pairlist = self.filter_pairlist(_pairlist, tickers)
            self._marketcap_cache["pairlist_mc"] = pairlist.copy()

        return pairlist

    def resolve_marketcap_pair(
        self,
        pair: str,
        pairlist: list[str],
        markets: list[str],
        filtered_pairlist: list[str],
    ) -> str | None:
        if pair in filtered_pairlist:
            return None

        if pair in pairlist:
            return pair

        if pair not in markets:
            for prefix in PairPrefixes:
                test_prefix = f"{prefix}{pair}"

                if test_prefix in pairlist:
                    return test_prefix

        return None

    def filter_pairlist(self, pairlist: list[str], tickers: dict) -> list[str]:
        """
        Filters and sorts pairlist and returns the whitelist again.
        Called on each bot iteration - please use internal caching if necessary
        :param pairlist: pairlist to filter or sort
        :param tickers: Tickers (from exchange.get_tickers). May be cached.
        :return: new whitelist
        """
        marketcap_list = self._marketcap_cache.get("marketcap")
        if (self._last_good_data is not None
                and (self._last_good_at == 0
                     or time.monotonic() - self._last_good_at > self._fallback_max_age)):
            marketcap_list = None
        mode = self._mode
        is_whitelist_mode = mode == "whitelist"
        filtered_pairlist: list[str] = []

        default_kwargs = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": "250",
            "page": "1",
            "sparkline": "false",
            "locale": "en",
        }

        if marketcap_list is None:
            data = []

            try:
                if not self._categories:
                    pages_required = math.ceil(self._max_rank / 250)
                    for page in range(1, pages_required + 1):
                        default_kwargs["page"] = str(page)
                        page_data = self._coingecko.get_coins_markets(**default_kwargs)
                        if not page_data:
                            raise ValueError("Empty CoinGecko response")
                        data.extend(page_data)
                else:
                    for category in self._categories:
                        category_data = self._coingecko.get_coins_markets(
                            **default_kwargs, **({"category": category} if category else {})
                        )
                        if not category_data:
                            raise ValueError("Empty CoinGecko response")
                        data += category_data
                if not data or any(not isinstance(row.get("symbol"), str) for row in data):
                    raise ValueError("Invalid CoinGecko response")
                self._last_good_data = data.copy()
                self._last_good_at = time.monotonic()
                cache_path = getattr(self, "_cache_file", None)
                if cache_path is not None:
                    try:
                        cache_path.parent.mkdir(parents=True, exist_ok=True)
                        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
                        tmp.write_text(
                            json.dumps({"saved_epoch": time.time(), "data": self._last_good_data})
                        )
                        tmp.replace(cache_path)
                    except OSError as io_err:
                        logger.warning("Could not persist CoinGecko cache file %s: %s", cache_path, io_err)
            except Exception as exc:
                if (self._last_good_data is None
                        or self._last_good_at == 0
                        or time.monotonic() - self._last_good_at > self._fallback_max_age):
                    cache_path = getattr(self, "_cache_file", None)
                    loaded_disk = False
                    if cache_path is not None and cache_path.is_file():
                        try:
                            payload = json.loads(cache_path.read_text())
                            saved_epoch = float(payload.get("saved_epoch", 0.0))
                            disk_data = payload.get("data")
                            age = time.time() - saved_epoch
                            if (
                                isinstance(disk_data, list)
                                and disk_data
                                and all(isinstance(row, dict)
                                        and isinstance(row.get("symbol"), str)
                                        and row["symbol"].strip()
                                        and isinstance(row.get("market_cap"), (int, float))
                                        and not isinstance(row["market_cap"], bool)
                                        and math.isfinite(row["market_cap"])
                                        and row["market_cap"] >= 0
                                        for row in disk_data)
                                and 0 <= age <= self._fallback_max_age
                            ):
                                self._last_good_data = disk_data
                                # A cache can predate the current system boot. Preserve its age.
                                self._last_good_at = time.monotonic() - age
                                loaded_disk = True
                        except Exception:
                            loaded_disk = False
                    if not loaded_disk:
                        raise OperationalException(
                            "Market-cap evidence unavailable: no unverified volume fallback allowed"
                        ) from exc
                logger.warning("CoinGecko unavailable; using bounded last-known market caps")
                data = self._last_good_data.copy()

            data.sort(key=lambda d: float(d.get("market_cap") or 0.0), reverse=True)

            if data:
                marketcap_list = [row["symbol"] for row in data
                                  if float(row.get("market_cap") or 0) >= self._min_market_cap]
                self._marketcap_cache["marketcap"] = marketcap_list

        if marketcap_list:
            market = self._exchange._config["trading_mode"]
            pair_format = f"{self._stake_currency.upper()}" + (
                f":{self._stake_currency.upper()}" if market == "futures" else ""
            )

            top_marketcap = marketcap_list[: self._max_rank :]
            markets = self.get_markets_exchange()

            for mc_pair in top_marketcap:
                pair = f"{mc_pair.upper()}/{pair_format}"
                resolved = self.resolve_marketcap_pair(pair, pairlist, markets, filtered_pairlist)

                if resolved:
                    if not is_whitelist_mode:
                        pairlist.remove(resolved)
                        continue

                    filtered_pairlist.append(resolved)
                    if not getattr(self, "_preserve_order", False) and len(filtered_pairlist) == self._number_assets:
                        break

            if is_whitelist_mode and getattr(self, "_preserve_order", False):
                allowed = set(filtered_pairlist)
                filtered_pairlist = [p for p in pairlist if p in allowed][: self._number_assets]

        if not is_whitelist_mode:
            return pairlist

        # If no pairs are found, return the original pairlist
        return filtered_pairlist
