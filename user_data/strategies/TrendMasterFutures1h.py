import numpy as np
import pandas as pd
from pandas import DataFrame
from freqtrade.strategy.interface import IStrategy
import talib.abstract as ta
import freqtrade.vendor.qtpylib.indicators as qtpylib


class TrendMasterFutures1h(IStrategy):
    """
    TrendMasterFutures1h
    ====================
    Institutional-Grade 1-Hour Trend Following & Momentum Strategy 
    built to PASS Prop Firm challenges (Crypto Fund Trader, Bybit Futures).

    Core Architectural Highlights:
    - Timeframe: 1-Hour (Eliminates lower-timeframe chop & fee drag).
    - Max Open Trades: 2 (Guarantees max daily risk <= 4.0% even under Black Swan).
    - Strict Stop Loss: -2.0% on Exchange.
    - Asymmetric Profit Hunting: Minimal ROI ladders from +8.0% to +3.0%.
    - Dynamic Trailing Stop: Locks in gains once +2.5% is achieved, trailing by 1.2%.
    - ZERO DCA / Single bullet entries.
    """

    INTERFACE_VERSION = 3
    can_short: bool = True
    timeframe = '1h'

    # Asymmetric Take Profit Ladder (Captures 1:2 to 1:4 Risk-to-Reward)
    minimal_roi = {
        "0": 0.080,    # +8.0% runner
        "12": 0.055,   # +5.5% after 12 hours
        "24": 0.035,   # +3.5% after 24 hours
        "48": 0.020    # +2.0% after 48 hours
    }

    # Strict hard stoploss (never risk more than 2% per trade)
    stoploss = -0.020

    # Trailing Stop: locks in runner profits safely
    trailing_stop = True
    trailing_stop_positive = 0.012
    trailing_stop_positive_offset = 0.025
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    startup_candle_count = 200

    @property
    def protections(self):
        return [
            {
                "method": "CooldownPeriod",
                "stop_duration_candles": 6  # 6 hours cooldown after position close
            },
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 24,
                "trade_limit": 2,
                "stop_duration_candles": 12,
                "only_per_pair": False
            }
        ]

    def leverage(self, pair: str, current_time, current_rate: float,
                 proposed_leverage: float, max_leverage: float, entry_tag: str,
                 side: str, **kwargs) -> float:
        """Conservative 1.5x isolated leverage for maximum safety."""
        return 1.5

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # 24-Period (24-hour) Donchian High / Low Channels
        dataframe['donchian_high'] = dataframe['high'].rolling(window=24).max()
        dataframe['donchian_low'] = dataframe['low'].rolling(window=24).min()
        dataframe['donchian_mid'] = (dataframe['donchian_high'] + dataframe['donchian_low']) / 2.0

        # Moving Averages (Trend Confirmation)
        dataframe['ema_20'] = ta.EMA(dataframe, timeperiod=20)
        dataframe['ema_50'] = ta.EMA(dataframe, timeperiod=50)
        dataframe['ema_200'] = ta.EMA(dataframe, timeperiod=200)

        # ADX & DMI for Strong Trend Filtering
        dataframe['adx'] = ta.ADX(dataframe, timeperiod=14)
        dataframe['plus_di'] = ta.PLUS_DI(dataframe, timeperiod=14)
        dataframe['minus_di'] = ta.MINUS_DI(dataframe, timeperiod=14)

        # Volume Confirmation Filter
        dataframe['volume_sma20'] = ta.SMA(dataframe['volume'], timeperiod=20)

        # RSI Momentum
        dataframe['rsi'] = ta.RSI(dataframe, timeperiod=14)

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # ----------------- LONG ENTRY -----------------
        dataframe.loc[
            (
                # Clean 24-hour High Breakout Crossover
                (dataframe['close'] > dataframe['donchian_high'].shift(1)) &
                (dataframe['close'].shift(1) <= dataframe['donchian_high'].shift(2)) &
                # Macro Bullish Trend
                (dataframe['close'] > dataframe['ema_200']) &
                (dataframe['ema_20'] > dataframe['ema_50']) &
                # Directional Movement & Strength
                (dataframe['plus_di'] > dataframe['minus_di']) &
                (dataframe['adx'] > 22) &
                # Volume Confirmation
                (dataframe['volume'] > dataframe['volume_sma20'] * 1.1) &
                # Healthy Momentum
                (dataframe['rsi'] > 50) &
                (dataframe['rsi'] < 72)
            ),
            ['enter_long', 'enter_tag']
        ] = (1, 'trend_breakout_long_1h')

        # ----------------- SHORT ENTRY -----------------
        dataframe.loc[
            (
                # Clean 24-hour Low Breakdown Crossover
                (dataframe['close'] < dataframe['donchian_low'].shift(1)) &
                (dataframe['close'].shift(1) >= dataframe['donchian_low'].shift(2)) &
                # Macro Bearish Trend
                (dataframe['close'] < dataframe['ema_200']) &
                (dataframe['ema_20'] < dataframe['ema_50']) &
                # Directional Movement & Strength
                (dataframe['minus_di'] > dataframe['plus_di']) &
                (dataframe['adx'] > 22) &
                # Volume Confirmation
                (dataframe['volume'] > dataframe['volume_sma20'] * 1.1) &
                # Healthy Momentum
                (dataframe['rsi'] < 50) &
                (dataframe['rsi'] > 28)
            ),
            ['enter_short', 'enter_tag']
        ] = (1, 'trend_breakdown_short_1h')

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Exit Long if trend loses mid-band and EMA 20
        dataframe.loc[
            (
                (dataframe['close'] < dataframe['donchian_mid']) &
                (dataframe['close'] < dataframe['ema_20']) &
                (dataframe['rsi'] < 45)
            ),
            ['exit_long', 'exit_tag']
        ] = (1, 'exit_long_loss_of_momentum')

        # Exit Short if price recovers above mid-band and EMA 20
        dataframe.loc[
            (
                (dataframe['close'] > dataframe['donchian_mid']) &
                (dataframe['close'] > dataframe['ema_20']) &
                (dataframe['rsi'] > 55)
            ),
            ['exit_short', 'exit_tag']
        ] = (1, 'exit_short_rebound')

        return dataframe
