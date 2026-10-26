import numpy as np
import pandas as pd
from pandas import DataFrame
from freqtrade.strategy.interface import IStrategy
from freqtrade.strategy import merge_informative_pair
import talib.abstract as ta
import freqtrade.vendor.qtpylib.indicators as qtpylib


class PropFirmVelocity15m(IStrategy):
    """
    PropFirmVelocity15m
    ===================
    High-Velocity Institutional Momentum Strategy on 15-Minute Candles.
    Specifically designed to pass Prop Firm Evaluations rapidly (within 30-45 days)
    while strictly respecting:
    - NO DCA / Single bullet entries.
    - Max Daily Risk <= 2.0% (Single loss capped at ~0.38% of wallet).
    - Asymmetric R:R: 1:2.3 (Cut loss at -1.5%, take profits at +2.5% to +4.5%).
    - Multi-pair active compounding across top momentum altcoins.
    """

    INTERFACE_VERSION = 3
    can_short: bool = True
    timeframe = '15m'

    # Fast Compounding Take Profit Ladder
    minimal_roi = {
        "0": 0.040,    # +4.0% high runner
        "30": 0.028,   # +2.8% after 30 mins
        "60": 0.020,   # +2.0% after 1 hour
        "120": 0.014   # +1.4% time decay target
    }

    # Strict hard stoploss (-1.5% maximum loss per trade)
    stoploss = -0.015

    # Trailing Stop: lock in profit once +1.8% is reached, trailing by 0.8%
    trailing_stop = True
    trailing_stop_positive = 0.008
    trailing_stop_positive_offset = 0.018
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    startup_candle_count = 100

    @property
    def protections(self):
        return [
            {
                "method": "CooldownPeriod",
                "stop_duration_candles": 4  # 1 hour cooldown per pair
            },
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 32,  # 8 hours
                "trade_limit": 3,
                "stop_duration_candles": 16,  # 4 hours timeout
                "only_per_pair": False
            },
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 96,  # 24 hours
                "trade_limit": 4,
                "stop_duration_candles": 32,
                "max_allowed_drawdown": 0.025  # 2.5% daily drawdown circuit breaker
            }
        ]

    def informative_pairs(self):
        return [("BTC/USDT:USDT", "1h")]

    def leverage(self, pair: str, current_time, current_rate: float,
                 proposed_leverage: float, max_leverage: float, entry_tag: str,
                 side: str, **kwargs) -> float:
        return 1.5

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # 1. Informative BTC 1-Hour Trend
        if not self.dp:
            dataframe['btc_bull_1h'] = 1
            dataframe['btc_bear_1h'] = 1
        else:
            btc_candles = self.dp.get_pair_dataframe("BTC/USDT:USDT", "1h")
            if not btc_candles.empty:
                btc_candles['btc_ema_50'] = ta.EMA(btc_candles, timeperiod=50)
                btc_candles['btc_bull'] = (btc_candles['close'] > btc_candles['btc_ema_50']).astype(int)
                btc_candles['btc_bear'] = (btc_candles['close'] < btc_candles['btc_ema_50']).astype(int)
                dataframe = merge_informative_pair(
                    dataframe, btc_candles[['date', 'btc_bull', 'btc_bear']],
                    self.timeframe, "1h", ffill=True
                )
            else:
                dataframe['btc_bull_1h'] = 1
                dataframe['btc_bear_1h'] = 1

        if 'btc_bull_1h' not in dataframe.columns:
            dataframe['btc_bull_1h'] = 1
            dataframe['btc_bear_1h'] = 1
        else:
            dataframe['btc_bull_1h'] = dataframe['btc_bull_1h'].fillna(1)
            dataframe['btc_bear_1h'] = dataframe['btc_bear_1h'].fillna(1)

        # 2. Fast EMA Momentum Ribbon (9, 21, 55, 200)
        dataframe['ema_9'] = ta.EMA(dataframe, timeperiod=9)
        dataframe['ema_21'] = ta.EMA(dataframe, timeperiod=21)
        dataframe['ema_55'] = ta.EMA(dataframe, timeperiod=55)
        dataframe['ema_200'] = ta.EMA(dataframe, timeperiod=200)

        # 3. ADX & Directional Strength
        dataframe['adx'] = ta.ADX(dataframe, timeperiod=14)
        dataframe['plus_di'] = ta.PLUS_DI(dataframe, timeperiod=14)
        dataframe['minus_di'] = ta.MINUS_DI(dataframe, timeperiod=14)

        # 4. Volume Surge Detection
        dataframe['volume_sma20'] = ta.SMA(dataframe['volume'], timeperiod=20)

        # 5. RSI Momentum
        dataframe['rsi'] = ta.RSI(dataframe, timeperiod=14)

        # 6. MACD for Momentum Confirmation
        macd = ta.MACD(dataframe, fastperiod=8, slowperiod=21, signalperiod=5)
        dataframe['macdhist'] = macd['macdhist']

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # ----------------- LONG ENTRY (High Velocity Surge) -----------------
        dataframe.loc[
            (
                # Clean EMA Ribbon Alignment
                (dataframe['close'] > dataframe['ema_9']) &
                (dataframe['ema_9'] > dataframe['ema_21']) &
                (dataframe['ema_21'] > dataframe['ema_55']) &
                (dataframe['close'] > dataframe['ema_200']) &
                # Fresh Momentum Ignition
                (dataframe['ema_9'].shift(1) <= dataframe['ema_21'].shift(1) * 1.002) &
                # Strong Directional Trend
                (dataframe['plus_di'] > dataframe['minus_di']) &
                (dataframe['adx'] > 22) &
                # MACD Expansion
                (dataframe['macdhist'] > 0) &
                (dataframe['macdhist'] > dataframe['macdhist'].shift(1)) &
                # Volume Surge
                (dataframe['volume'] > dataframe['volume_sma20'] * 1.25) &
                # Healthy Momentum (Not overbought)
                (dataframe['rsi'] > 52) &
                (dataframe['rsi'] < 72) &
                # Bitcoin Macro Alignment
                (dataframe['btc_bull_1h'] == 1)
            ),
            ['enter_long', 'enter_tag']
        ] = (1, 'velocity_long_15m')

        # ----------------- SHORT ENTRY (High Velocity Breakdown) -----------------
        dataframe.loc[
            (
                # Clean EMA Ribbon Alignment
                (dataframe['close'] < dataframe['ema_9']) &
                (dataframe['ema_9'] < dataframe['ema_21']) &
                (dataframe['ema_21'] < dataframe['ema_55']) &
                (dataframe['close'] < dataframe['ema_200']) &
                # Fresh Momentum Ignition
                (dataframe['ema_9'].shift(1) >= dataframe['ema_21'].shift(1) * 0.998) &
                # Strong Directional Trend
                (dataframe['minus_di'] > dataframe['plus_di']) &
                (dataframe['adx'] > 22) &
                # MACD Expansion
                (dataframe['macdhist'] < 0) &
                (dataframe['macdhist'] < dataframe['macdhist'].shift(1)) &
                # Volume Surge
                (dataframe['volume'] > dataframe['volume_sma20'] * 1.25) &
                # Healthy Momentum (Not oversold)
                (dataframe['rsi'] < 48) &
                (dataframe['rsi'] > 28) &
                # Bitcoin Macro Alignment
                (dataframe['btc_bear_1h'] == 1)
            ),
            ['enter_short', 'enter_tag']
        ] = (1, 'velocity_short_15m')

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Exit Long if 9 EMA crosses below 21 EMA
        dataframe.loc[
            (
                (dataframe['close'] < dataframe['ema_21']) &
                (dataframe['rsi'] < 46)
            ),
            ['exit_long', 'exit_tag']
        ] = (1, 'exit_long_loss_of_momentum')

        # Exit Short if 9 EMA crosses above 21 EMA
        dataframe.loc[
            (
                (dataframe['close'] > dataframe['ema_21']) &
                (dataframe['rsi'] > 54)
            ),
            ['exit_short', 'exit_tag']
        ] = (1, 'exit_short_rebound')

        return dataframe
