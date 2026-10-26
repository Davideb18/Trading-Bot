import numpy as np
import pandas as pd
from pandas import DataFrame
from freqtrade.strategy.interface import IStrategy
import talib.abstract as ta
import freqtrade.vendor.qtpylib.indicators as qtpylib


def calculate_var_ott(dataframe: DataFrame, period: int = 14, percent: float = 1.4) -> DataFrame:
    """
    Optimized Trend Tracker (OTT) calculation.
    Uses Variable Moving Average (VIDYA / VAR) as support line.
    """
    close = dataframe['close']
    cmo = ta.CMO(dataframe, timeperiod=period)
    abs_cmo = np.abs(cmo) / 100.0

    # Variable Moving Average approximation
    sc = 2.0 / (period + 1)
    var = pd.Series(close.values, index=dataframe.index)

    for i in range(1, len(dataframe)):
        var.iloc[i] = (sc * abs_cmo.iloc[i] * close.iloc[i]) + ((1.0 - (sc * abs_cmo.iloc[i])) * var.iloc[i - 1])

    dataframe['var'] = var
    fark = var * (percent / 100.0)
    dataframe['ott_upper'] = var + fark
    dataframe['ott_lower'] = var - fark

    ott = pd.Series(var.values, index=dataframe.index)
    for i in range(1, len(dataframe)):
        prev_ott = ott.iloc[i - 1]
        curr_var = var.iloc[i]
        curr_upper = dataframe['ott_upper'].iloc[i]
        curr_lower = dataframe['ott_lower'].iloc[i]

        if curr_var > prev_ott:
            ott.iloc[i] = max(curr_lower, prev_ott)
        else:
            ott.iloc[i] = min(curr_upper, prev_ott)

    dataframe['ott'] = ott
    return dataframe


class FOttFuturesStrategy(IStrategy):
    """
    FOttFuturesStrategy
    ===================
    Optimized Trend Tracker (OTT) Futures Strategy tailored for Prop Firm Challenges.
    Adapted from the official Freqtrade Futures Community repository.
    
    Features:
    - can_short = True
    - Strict Stop Loss: -0.015 (-1.5%)
    - ADX Filter > 25 to avoid chop
    - Zero DCA / No averaging down
    - Leverage: 2.0x isolated
    """

    INTERFACE_VERSION = 3
    can_short: bool = True
    timeframe = '15m'

    minimal_roi = {
        "0": 0.035,   # +3.5%
        "45": 0.022,  # +2.2%
        "90": 0.014,  # +1.4%
        "180": 0.005  # Scratch
    }

    stoploss = -0.015

    trailing_stop = True
    trailing_stop_positive = 0.008
    trailing_stop_positive_offset = 0.015
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    startup_candle_count = 100

    def leverage(self, pair: str, current_time, current_rate: float,
                 proposed_leverage: float, max_leverage: float, entry_tag: str,
                 side: str, **kwargs) -> float:
        return 2.0

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Calculate VAR and OTT
        dataframe = calculate_var_ott(dataframe, period=14, percent=1.4)

        # ADX for trend strength filter
        dataframe['adx'] = ta.ADX(dataframe, timeperiod=14)

        # RSI Momentum
        dataframe['rsi'] = ta.RSI(dataframe, timeperiod=14)

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # ----------------- LONG ENTRY -----------------
        dataframe.loc[
            (
                # VAR crosses above OTT
                qtpylib.crossed_above(dataframe['var'], dataframe['ott']) &
                # Strong trend confirmed
                (dataframe['adx'] > 24) &
                (dataframe['rsi'] > 50) &
                (dataframe['rsi'] < 70)
            ),
            ['enter_long', 'enter_tag']
        ] = (1, 'ott_cross_long')

        # ----------------- SHORT ENTRY -----------------
        dataframe.loc[
            (
                # VAR crosses below OTT
                qtpylib.crossed_below(dataframe['var'], dataframe['ott']) &
                # Strong trend confirmed
                (dataframe['adx'] > 24) &
                (dataframe['rsi'] < 50) &
                (dataframe['rsi'] > 30)
            ),
            ['enter_short', 'enter_tag']
        ] = (1, 'ott_cross_short')

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Exit Long
        dataframe.loc[
            (
                qtpylib.crossed_below(dataframe['var'], dataframe['ott'])
            ),
            ['exit_long', 'exit_tag']
        ] = (1, 'ott_exit_long')

        # Exit Short
        dataframe.loc[
            (
                qtpylib.crossed_above(dataframe['var'], dataframe['ott'])
            ),
            ['exit_short', 'exit_tag']
        ] = (1, 'ott_exit_short')

        return dataframe
