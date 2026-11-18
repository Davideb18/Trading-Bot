"""Exclude nonstandard Bybit instruments using exchange metadata, not ticker regexes."""

from freqtrade.plugins.pairlist.IPairList import IPairList, SupportsBacktesting


def eligible_instrument(info):
    if not isinstance(info, dict):
        return False
    # Missing classification is unknown. Bybit explicitly uses an empty string for crypto.
    if "symbolType" not in info or "tags" not in info:
        return False
    return (
        info.get("contractType") == "LinearPerpetual"
        and info.get("status") == "Trading"
        and info.get("quoteCoin") == "USDT"
        and info.get("settleCoin") == "USDT"
        and str(info["symbolType"]).lower() == ""
        and isinstance(info["tags"], list)
        and not info["tags"]
        and not info.get("isPreListing", False)
    )


class PropInstrumentFilter(IPairList):
    supports_backtesting = SupportsBacktesting.NO

    @property
    def needstickers(self):
        return False

    def short_desc(self):
        return "Verified Bybit USDT perpetual crypto; exclude Innovation, ST and TradFi."

    @staticmethod
    def description():
        return "Require exchange metadata proving ordinary USDT perpetual crypto."

    def _validate_pair(self, pair, ticker):
        return eligible_instrument(self._exchange.markets.get(pair, {}).get("info"))
