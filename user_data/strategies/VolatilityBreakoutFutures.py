import numpy as np
import pandas as pd
from pandas import DataFrame
from freqtrade.strategy.interface import IStrategy
import talib.abstract as ta
import freqtrade.vendor.qtpylib.indicators as qtpylib


class VolatilityBreakoutFutures(IStrategy):
    """
    VolatilityBreakoutFutures (Prop-Firm Institutional Edition)
    ===========================================================
    Disciplined directional breakout strategy designed specifically 
    to pass Prop Firm challenges without over-trading or breaching daily drawdown.

    Key Principles:
    - Pure Crossover Trigger: Enters ONLY on the initial breakout candle.
    - 48-Period (12h) High/Low Channel with 1.2x Volume Confirmation.
    - Strict Macro EMA 200 Trend Filter (Only Long above 200 EMA, Only Short below 200 EMA).
    - Hard Stop Loss on Exchange: -2.0% (Risk max 2% per trade, max 3 trades = 6% extreme cap).
    - Asymmetric Profit Target: +4.5% Take Profit (R:R >= 1:2.25).
    - Trailing Stop: Activates at +2.0%, trailing by 1.0% to lock in risk-free runners.
    - NO DCA, NO Martingale: 1 position per asset.
    """

    INTERFACE_VERSION = 3
    can_short: bool = True
    timeframe = '15m'

    # Target +4.5% profit, decay to +2.5% after 2 hours
    minimal_roi = {
        "0": 0.045,
        "120": 0.030,
        "240": 0.020,
        "480": 0.010
    }

    # Strict Stop Loss (Prop firm safe)
    stoploss = -0.020

    # Trailing Stop: locks in profit once trade hits +2.0%
    trailing_stop = True
    trailing_stop_positive = 0.010
    trailing_stop_positive_offset = 0.020
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    startup_candle_count = 200

    @property
    def protections(self):
        return [
            {
                "method": "CooldownPeriod",
                "stop_duration_candles": 16  # 4 hours cooldown after trade close
            },
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 48,
                "trade_limit": 2,
                "stop_duration_candles": 32,
                "only_per_pair": False
            }
        ]

    def leverage(self, pair: str, current_time, current_rate: float,
                 proposed_leverage: float, max_leverage: float, entry_tag: str,
                 side: str, **kwargs) -> float:
        return 2.0

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # 48-period (12-hour) Range High / Low
        dataframe['range_high'] = dataframe['high'].rolling(window=48).max()
        dataframe['range_low'] = dataframe['low'].rolling(window=48).min()
        dataframe['range_mid'] = (dataframe['range_high'] + dataframe['range_low']) / 2.0

        # Trend Hierarchy
        dataframe['ema_50'] = ta.EMA(dataframe, timeperiod=50)
        dataframe['ema_200'] = ta.EMA(dataframe, timeperiod=200)

        # Volume Confirmation
        dataframe['volume_sma'] = ta.SMA(dataframe['volume'], timeperiod=30)

        # ATR & ADX for trend strength
        dataframe['atr'] = ta.ATR(dataframe, timeperiod=14)
        dataframe['adx'] = ta.ADX(dataframe, timeperiod=14)
        dataframe['rsi'] = ta.RSI(dataframe, timeperiod=14)

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # ----------------- LONG ENTRY -----------------
        dataframe.loc[
            (
                # Pristine Breakout Crossover (only triggers on the exact breakthrough candle)
                (dataframe['close'] > dataframe['range_high'].shift(1)) &
                (dataframe['close'].shift(1) <= dataframe['range_high'].shift(2)) &
                # Macro Bullish Trend
                (dataframe['close'] > dataframe['ema_200']) &
                (dataframe['ema_50'] > dataframe['ema_200']) &
                # Volume & Momentum confirmation
                (dataframe['volume'] > dataframe['volume_sma'] * 1.2) &
                (dataframe['adx'] > 20) &
                (dataframe['rsi'] > 52) &
                (dataframe['rsi'] < 72)
            ),
            ['enter_long', 'enter_tag']
        ] = (1, 'breakout_crossover_long')

        # ----------------- SHORT ENTRY -----------------
        dataframe.loc[
            (
                # Pristine Breakdown Crossover
                (dataframe['close'] < dataframe['range_low'].shift(1)) &
                (dataframe['close'].shift(1) >= dataframe['range_low'].shift(2)) &
                # Macro Bearish Trend
                (dataframe['close'] < dataframe['ema_200']) &
                (dataframe['close'] < dataframe['ema_50']) &
                (dataframe['ema_50'] < dataframe['ema_200']) &
                # Volume & Momentum confirmation
                (dataframe['volume'] > dataframe['volume_sma'] * 1.2) &
                (dataframe['adx'] > 20) &
                (dataframe['rsi'] < 48) &
                (dataframe['rsi'] > 28)
            ),
            ['enter_short', 'enter_tag']
        ] = (1, 'breakdown_crossover_short')

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Exit Long on mid-band breakdown with bearish momentum
        dataframe.loc[
            (
                (dataframe['close'] < dataframe['range_mid']) &
                (dataframe['rsi'] < 42)
            ),
            ['exit_long', 'exit_tag']
        ] = (1, 'exit_long_range_loss')

        # Exit Short on mid-band reclaim with bullish momentum
        dataframe.loc[
            (
                (dataframe['close'] > dataframe['range_mid']) &
                (dataframe['rsi'] > 58)
            ),
            ['exit_short', 'exit_tag']
        ] = (1, 'exit_short_range_reclaim')

        return dataframe
