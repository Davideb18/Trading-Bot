import numpy as np
import pandas as pd
from pandas import DataFrame
from freqtrade.strategy.interface import IStrategy
import talib.abstract as ta


def calculate_supertrend(dataframe: DataFrame, multiplier: float = 3.0, period: int = 10) -> DataFrame:
    """Calculates vectorized SuperTrend indicator."""
    hl2 = (dataframe['high'] + dataframe['low']) / 2.0
    atr = ta.ATR(dataframe, timeperiod=period)

    upperband = hl2 + (multiplier * atr)
    lowerband = hl2 - (multiplier * atr)

    supertrend = pd.Series(True, index=dataframe.index)
    trend = 1
    final_upperband = upperband.copy()
    final_lowerband = lowerband.copy()

    for i in range(1, len(dataframe)):
        curr_close = dataframe['close'].iloc[i]
        prev_close = dataframe['close'].iloc[i - 1]
        curr_upper = upperband.iloc[i]
        prev_upper = final_upperband.iloc[i - 1]
        curr_lower = lowerband.iloc[i]
        prev_lower = final_lowerband.iloc[i - 1]

        if curr_lower > prev_lower or prev_close < prev_lower:
            final_lowerband.iloc[i] = curr_lower
        else:
            final_lowerband.iloc[i] = prev_lower

        if curr_upper < prev_upper or prev_close > prev_upper:
            final_upperband.iloc[i] = curr_upper
        else:
            final_upperband.iloc[i] = prev_upper

        if trend == 1:
            if curr_close < final_lowerband.iloc[i]:
                trend = -1
        else:
            if curr_close > final_upperband.iloc[i]:
                trend = 1

        supertrend.iloc[i] = (trend == 1)

    dataframe['supertrend_is_bull'] = supertrend
    dataframe['supertrend_upper'] = final_upperband
    dataframe['supertrend_lower'] = final_lowerband
    return dataframe


class SuperTrendMomentumFutures(IStrategy):
    """
    SuperTrendMomentumFutures
    =========================
    Trend-Following & MACD Momentum Strategy tailored for Prop Firm Challenges.
    
    Key Features:
    - can_short = True (Captures both Bull and Bear trends)
    - Zero DCA: single position sizing ensuring fixed downside risk
    - Stoploss: -0.018 (-1.8%)
    - Profit Target: +4.0% (R:R > 1:2.2)
    - Confluence of SuperTrend (10, 3.0) + EMA 21/55 Alignment + MACD Histogram
    """

    INTERFACE_VERSION = 3
    can_short: bool = True
    timeframe = '15m'

    minimal_roi = {
        "0": 0.040,   # Target +4% profit
        "45": 0.025,  # Target +2.5%
        "90": 0.015,  # Lock in +1.5%
        "180": 0.005  # Scratch trade
    }

    stoploss = -0.018

    trailing_stop = True
    trailing_stop_positive = 0.008
    trailing_stop_positive_offset = 0.016
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    startup_candle_count = 100

    def leverage(self, pair: str, current_time, current_rate: float,
                 proposed_leverage: float, max_leverage: float, entry_tag: str,
                 side: str, **kwargs) -> float:
        return 2.0

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # SuperTrend (Period 10, Multiplier 3.0)
        dataframe = calculate_supertrend(dataframe, multiplier=3.0, period=10)

        # EMAs for Trend Hierarchy
        dataframe['ema_21'] = ta.EMA(dataframe, timeperiod=21)
        dataframe['ema_55'] = ta.EMA(dataframe, timeperiod=55)
        dataframe['ema_200'] = ta.EMA(dataframe, timeperiod=200)

        # MACD (12, 26, 9)
        macd = ta.MACD(dataframe)
        dataframe['macd'] = macd['macd']
        dataframe['macdsignal'] = macd['macdsignal']
        dataframe['macdhist'] = macd['macdhist']

        # RSI Momentum
        dataframe['rsi'] = ta.RSI(dataframe, timeperiod=14)

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # ----------------- LONG ENTRY -----------------
        dataframe.loc[
            (
                # SuperTrend is Bullish
                (dataframe['supertrend_is_bull'] == True) &
                (dataframe['supertrend_is_bull'].shift(1) == False) &  # Fresh flip
                # EMA trend alignment
                (dataframe['close'] > dataframe['ema_21']) &
                (dataframe['ema_21'] > dataframe['ema_55']) &
                # MACD momentum confirmation
                (dataframe['macdhist'] > 0) &
                (dataframe['rsi'] > 50) &
                (dataframe['rsi'] < 72)
            ),
            ['enter_long', 'enter_tag']
        ] = (1, 'supertrend_flip_long')

        # ----------------- SHORT ENTRY -----------------
        dataframe.loc[
            (
                # SuperTrend is Bearish
                (dataframe['supertrend_is_bull'] == False) &
                (dataframe['supertrend_is_bull'].shift(1) == True) &  # Fresh flip
                # EMA trend alignment
                (dataframe['close'] < dataframe['ema_21']) &
                (dataframe['ema_21'] < dataframe['ema_55']) &
                # MACD momentum confirmation
                (dataframe['macdhist'] < 0) &
                (dataframe['rsi'] < 50) &
                (dataframe['rsi'] > 28)
            ),
            ['enter_short', 'enter_tag']
        ] = (1, 'supertrend_flip_short')

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Exit Long on SuperTrend Bearish Flip
        dataframe.loc[
            (
                (dataframe['supertrend_is_bull'] == False)
            ),
            ['exit_long', 'exit_tag']
        ] = (1, 'supertrend_exit_long')

        # Exit Short on SuperTrend Bullish Flip
        dataframe.loc[
            (
                (dataframe['supertrend_is_bull'] == True)
            ),
            ['exit_short', 'exit_tag']
        ] = (1, 'supertrend_exit_short')

        return dataframe
