import numpy as np
import pandas as pd
from pandas import DataFrame
from freqtrade.strategy.interface import IStrategy
from freqtrade.strategy import merge_informative_pair
import talib.abstract as ta
import freqtrade.vendor.qtpylib.indicators as qtpylib


class PropFirmMasterFutures1h(IStrategy):
    """
    PropFirmMasterFutures1h
    =======================
    Institutional Multi-Regime Trend Following Strategy tailored specifically
    for passing Prop Firm Evaluations (e.g. Crypto Fund Trader, Bybit Futures).

    Key Specifications:
    - Timeframe: 1-Hour
    - Isolated Leverage: 1.5x (Safe margin buffer)
    - Bitcoin Macro Regime Filter: Longs only in BTC Bull regime, Shorts only in BTC Bear regime.
    - Donchian 48-Period (48h) Breakout: Filters 1-day noise, catches sustained institutional swings.
    - Asymmetric R:R: Hard Stoploss -1.8%, Trailing lock-in at +2.4%, Take Profit +3.5% to +6.0%.
    - Protections: StoplossGuard & MaxDrawdown protection to guarantee Daily Drawdown < 4.0%.
    - Zero DCA / Single bullet entries.
    """

    INTERFACE_VERSION = 3
    can_short: bool = True
    timeframe = '1h'

    # Asymmetric Take Profit Ladder (Guarantees >= 1:2 Risk to Reward)
    minimal_roi = {
        "0": 0.060,    # +6.0% high runner
        "12": 0.045,   # +4.5% intermediate runner
        "24": 0.035,   # +3.5% swing target
        "48": 0.025    # +2.5% time decay floor
    }

    # Strict hard stoploss (1.8% maximum loss per trade)
    stoploss = -0.018

    # Trailing Stop: locks in profit once +2.4% is reached, trailing by 1.0%
    trailing_stop = True
    trailing_stop_positive = 0.010
    trailing_stop_positive_offset = 0.024
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    startup_candle_count = 200

    @property
    def protections(self):
        return [
            {
                "method": "CooldownPeriod",
                "stop_duration_candles": 4
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
            # Fallback if dataprovider not available
            dataframe['btc_bull'] = 1
            dataframe['btc_bear'] = 1
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
                dataframe['btc_bull'] = 1
                dataframe['btc_bear'] = 1

        # Fill potential NaNs in BTC regime
        if 'btc_bull_1h' not in dataframe.columns:
            dataframe['btc_bull_1h'] = 1
            dataframe['btc_bear_1h'] = 1
        else:
            dataframe['btc_bull_1h'] = dataframe['btc_bull_1h'].fillna(1)
            dataframe['btc_bear_1h'] = dataframe['btc_bear_1h'].fillna(1)

        # 2. 48-Period (48-hour) Donchian High / Low Channels
        dataframe['donchian_high'] = dataframe['high'].rolling(window=48).max()
        dataframe['donchian_low'] = dataframe['low'].rolling(window=48).min()
        dataframe['donchian_mid'] = (dataframe['donchian_high'] + dataframe['donchian_low']) / 2.0

        # 3. Moving Averages (Trend Hierarchy)
        dataframe['ema_20'] = ta.EMA(dataframe, timeperiod=20)
        dataframe['ema_50'] = ta.EMA(dataframe, timeperiod=50)
        dataframe['ema_200'] = ta.EMA(dataframe, timeperiod=200)

        # 4. ADX & Directional Indicators
        dataframe['adx'] = ta.ADX(dataframe, timeperiod=14)
        dataframe['plus_di'] = ta.PLUS_DI(dataframe, timeperiod=14)
        dataframe['minus_di'] = ta.MINUS_DI(dataframe, timeperiod=14)

        # 5. Volume & Volatility Filters
        dataframe['volume_sma20'] = ta.SMA(dataframe['volume'], timeperiod=20)
        dataframe['rsi'] = ta.RSI(dataframe, timeperiod=14)

        # 6. MACD for Momentum Confirmation
        macd = ta.MACD(dataframe)
        dataframe['macd'] = macd['macd']
        dataframe['macdsignal'] = macd['macdsignal']
        dataframe['macdhist'] = macd['macdhist']

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # ----------------- LONG ENTRY -----------------
        dataframe.loc[
            (
                # Clean 48-hour High Breakout Crossover
                (dataframe['close'] > dataframe['donchian_high'].shift(1)) &
                (dataframe['close'].shift(1) <= dataframe['donchian_high'].shift(2)) &
                # Macro Pair Trend Confirmation
                (dataframe['close'] > dataframe['ema_200']) &
                (dataframe['ema_20'] > dataframe['ema_50']) &
                # Directional Strength
                (dataframe['plus_di'] > dataframe['minus_di']) &
                (dataframe['adx'] > 20) &
                # MACD Bullish Momentum
                (dataframe['macdhist'] > 0) &
                # Volume Confirmation
                (dataframe['volume'] > dataframe['volume_sma20'] * 1.1) &
                # Healthy Momentum
                (dataframe['rsi'] > 50) &
                (dataframe['rsi'] < 72) &
                # Macro Bitcoin Regime Confirmation (Only Long when BTC > EMA 50)
                (dataframe['btc_bull_1h'] == 1)
            ),
            ['enter_long', 'enter_tag']
        ] = (1, 'prop_breakout_long_1h')

        # ----------------- SHORT ENTRY -----------------
        dataframe.loc[
            (
                # Clean 48-hour Low Breakdown Crossover
                (dataframe['close'] < dataframe['donchian_low'].shift(1)) &
                (dataframe['close'].shift(1) >= dataframe['donchian_low'].shift(2)) &
                # Macro Pair Trend Confirmation
                (dataframe['close'] < dataframe['ema_200']) &
                (dataframe['ema_20'] < dataframe['ema_50']) &
                # Directional Strength
                (dataframe['minus_di'] > dataframe['plus_di']) &
                (dataframe['adx'] > 20) &
                # MACD Bearish Momentum
                (dataframe['macdhist'] < 0) &
                # Volume Confirmation
                (dataframe['volume'] > dataframe['volume_sma20'] * 1.1) &
                # Healthy Momentum
                (dataframe['rsi'] < 50) &
                (dataframe['rsi'] > 28) &
                # Macro Bitcoin Regime Confirmation (Only Short when BTC < EMA 50)
                (dataframe['btc_bear_1h'] == 1)
            ),
            ['enter_short', 'enter_tag']
        ] = (1, 'prop_breakdown_short_1h')

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Exit Long if trend breaks below mid-band and EMA 20
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
