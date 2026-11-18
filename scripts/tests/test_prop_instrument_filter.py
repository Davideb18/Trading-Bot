import pytest

from freqtrade.plugins.pairlist.PropInstrumentFilter import eligible_instrument


def instrument(**kwargs):
    return dict(contractType="LinearPerpetual", status="Trading", quoteCoin="USDT",
                settleCoin="USDT", **kwargs)


def test_explicit_standard_crypto():
    assert eligible_instrument(instrument(symbolType="", tags=[]))


@pytest.mark.parametrize("kind", ["innovation", "stock", "ETF", "commodity", "forex", "new-type"])
def test_dynamic_classification_excludes_future_tickers(kind):
    assert not eligible_instrument(instrument(symbolType=kind, tags=[]))


def test_missing_metadata_and_st_fail_closed():
    assert not eligible_instrument(instrument())
    assert not eligible_instrument(instrument(symbolType="", tags=["ST"]))
