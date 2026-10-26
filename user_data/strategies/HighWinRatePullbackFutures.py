import numpy as np
import pandas as pd
from pandas import DataFrame
from freqtrade.strategy.interface import IStrategy
from freqtrade.strategy import merge_informative_pair
import talib.abstract as ta
import freqtrade.vendor.qtpylib.indicators as qtpylib


class HighWinRatePullbackFutures(IStrategy):
    """
    HighWinRatePullbackFutures
    ==========================
    Institutional "Buy the Dip / Sell the Rip" Mean Reversion Strategy
    engineered specifically for Prop Firms requiring high win rates (65%-75%+)
    and smooth equity curves without DCA or Martingale.

    Core Philosophy:
    - Never chase breakouts at the top.
    - Identify dominant institutional trend (EMA 200 + BTC Macro Regime).
    - Wait for an oversold pullback to Bollinger Lower Band / EMA 50 in uptrend.
    - Wait for an overbought rally to Bollinger Upper Band in downtrend.
    - Enter on reversal confirmation (Stochastic crossover + candle reversal).
    - Quick asymmetric take-profit at the mean (+2.0% to +3.5%) with tight -1.8% Stop Loss.
    """

    INTERFACE_VERSION = 3
    can_short: bool = True
    timeframe = '1h'

    # Quick Mean-Reversion Profit Targets
    minimal_roi = {
        "0": 0.035,    # +3.5% quick target
        "6": 0.025,    # +2.5% after 6 hours
        "18": 0.018    # +1.8% after 18 hours
    }

    # Strict hard stoploss (never risk more than 1.8% per trade)
    stoploss = -0.018

    # Trailing Stop: lock in profit safely once +2.0% is reached
    trailing_stop = True
    trailing_stop_positive = 0.008
    trailing_stop_positive_offset = 0.020
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    startup_candle_count = 200

    @property
    def protections(self):
        return [
            {
                "method": "CooldownPeriod",
                "stop_duration_candles": 3
            },
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 24,
                "trade_limit": 2,
                "stop_duration_candles": 12,
                "only_per_pair": False
            },
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 48,
                "trade_limit": 3,
                "stop_duration_candles": 20,
                "max_allowed_drawdown": 0.03
            }
        ]

    def informative_pairs(self):
        """Define informative BTC pair for market regime classification."""
        return [("BTC/USDT:USDT", self.timeframe)]

    def leverage(self, pair: str, current_time, current_rate: float,
                 proposed_leverage: float, max_leverage: float, entry_tag: str,
                 side: str, **kwargs) -> float:
        """Conservative 1.5x isolated leverage for maximum safety."""
        return 1.5

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # 1. Informative BTC Macro Trend
        if not self.dp:
            dataframe['btc_bull_1h'] = 1
            dataframe['btc_bear_1h'] = 1
        else:
            btc_candles = self.dp.get_pair_dataframe("BTC/USDT:USDT", self.timeframe)
            if not btc_candles.empty:
                btc_candles['btc_ema_50'] = ta.EMA(btc_candles, timeperiod=50)
                btc_candles['btc_bull'] = (btc_candles['close'] > btc_candles['btc_ema_50']).astype(int)
                btc_candles['btc_bear'] = (btc_candles['close'] < btc_candles['btc_ema_50']).astype(int)
                dataframe = merge_informative_pair(
                    dataframe, btc_candles[['date', 'btc_bull', 'btc_bear']],
                    self.timeframe, self.timeframe, ffill=True
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

        # 2. Moving Averages for Trend Filtering
        dataframe['ema_20'] = ta.EMA(dataframe, timeperiod=20)
        dataframe['ema_50'] = ta.EMA(dataframe, timeperiod=50)
        dataframe['ema_100'] = ta.EMA(dataframe, timeperiod=100)
        dataframe['ema_200'] = ta.EMA(dataframe, timeperiod=200)

        # 3. Bollinger Bands (20 periods, 2.0 std)
        bollinger = qtpylib.bollinger_bands(qtpylib.typical_price(dataframe), window=20, stds=2.0)
        dataframe['bb_lower'] = bollinger['lower']
        dataframe['bb_mid'] = bollinger['mid']
        dataframe['bb_upper'] = bollinger['upper']

        # 4. RSI & Stochastic Momentum
        dataframe['rsi'] = ta.RSI(dataframe, timeperiod=14)
        stoch = ta.STOCH(dataframe, fastk_period=14, slowk_period=3, slowd_period=3)
        dataframe['slowk'] = stoch['slowk']
        dataframe['slowd'] = stoch['slowd']

        # 5. Volume Filter
        dataframe['volume_sma20'] = ta.SMA(dataframe['volume'], timeperiod=20)

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # ----------------- LONG ENTRY (Buy the Dip in Uptrend) -----------------
        dataframe.loc[
            (
                # Macro Bullish Trend
                (dataframe['close'] > dataframe['ema_100']) &
                (dataframe['ema_20'] > dataframe['ema_50']) &
                (dataframe['btc_bull_1h'] == 1) &
                # Oversold Pullback Condition
                (
                    (dataframe['low'] <= dataframe['bb_lower'] * 1.008) |
                    (dataframe['rsi'] < 42) |
                    (dataframe['low'] <= dataframe['ema_50'])
                ) &
                # Bullish Reversal Confirmation
                (dataframe['close'] > dataframe['open']) & # Green reversal candle
                (dataframe['rsi'] > dataframe['rsi'].shift(1)) & # RSI curving up
                (dataframe['slowk'] > dataframe['slowd']) & # Stochastic bullish cross
                (dataframe['slowk'] < 60) & # Not yet overbought
                # Volume confirmation
                (dataframe['volume'] > dataframe['volume_sma20'] * 0.7)
            ),
            ['enter_long', 'enter_tag']
        ] = (1, 'pullback_long_dip')

        # ----------------- SHORT ENTRY (Sell the Rip in Downtrend) -----------------
        dataframe.loc[
            (
                # Macro Bearish Trend
                (dataframe['close'] < dataframe['ema_100']) &
                (dataframe['ema_20'] < dataframe['ema_50']) &
                (dataframe['btc_bear_1h'] == 1) &
                # Overbought Rally Condition
                (
                    (dataframe['high'] >= dataframe['bb_upper'] * 0.992) |
                    (dataframe['rsi'] > 58) |
                    (dataframe['high'] >= dataframe['ema_50'])
                ) &
                # Bearish Reversal Confirmation
                (dataframe['close'] < dataframe['open']) & # Red reversal candle
                (dataframe['rsi'] < dataframe['rsi'].shift(1)) & # RSI curving down
                (dataframe['slowk'] < dataframe['slowd']) & # Stochastic bearish cross
                (dataframe['slowk'] > 40) & # Not yet oversold
                # Volume confirmation
                (dataframe['volume'] > dataframe['volume_sma20'] * 0.7)
            ),
            ['enter_short', 'enter_tag']
        ] = (1, 'pullback_short_rip')

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Exit Long when price touches Upper Band and RSI is overbought
        dataframe.loc[
            (
                (dataframe['close'] >= dataframe['bb_upper']) &
                (dataframe['rsi'] > 68)
            ),
            ['exit_long', 'exit_tag']
        ] = (1, 'exit_long_overbought')

        # Exit Short when price touches Lower Band and RSI is oversold
        dataframe.loc[
            (
                (dataframe['close'] <= dataframe['bb_lower']) &
                (dataframe['rsi'] < 32)
            ),
            ['exit_short', 'exit_tag']
        ] = (1, 'exit_short_oversold')

        return dataframe
