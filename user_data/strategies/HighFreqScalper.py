import numpy as np
import pandas as pd
from pandas import DataFrame
from freqtrade.strategy.interface import IStrategy
import talib.abstract as ta
import freqtrade.vendor.qtpylib.indicators as qtpylib

class HighFreqScalper(IStrategy):
    """
    Strategia ad alta frequenza creata su misura per estrarre profitto dai micro-movimenti.
    Sfrutta le Bande di Bollinger e l'RSI per entrare a mercato in modo molto aggressivo.
    """
    INTERFACE_VERSION = 3
    
    # ROI: Vogliamo profitti rapidi. Chiude al +2% in 20 minuti, o +1% in 60 min.
    minimal_roi = {
        "60": 0.01,
        "20": 0.02,
        "0": 0.04
    }

    # Stoploss stretto per limitare le perdite veloci
    stoploss = -0.05
    
    # Trailing stop
    trailing_stop = True
    trailing_stop_positive = 0.01
    trailing_stop_positive_offset = 0.02
    trailing_only_offset_is_reached = True

    timeframe = '5m'

    # Usa solo dati a breve termine, non ha bisogno di centinaia di giorni di storico
    startup_candle_count = 50

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # RSI
        dataframe['rsi'] = ta.RSI(dataframe, timeperiod=14)
        
        # Bollinger Bands (aggressive: 20 periods, 2 standard deviations)
        bollinger = qtpylib.bollinger_bands(qtpylib.typical_price(dataframe), window=20, stds=2)
        dataframe['bb_lowerband'] = bollinger['lower']
        dataframe['bb_middleband'] = bollinger['mid']
        dataframe['bb_upperband'] = bollinger['upper']
        
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                # Il prezzo chiude sotto la banda inferiore di Bollinger (forte calo)
                (dataframe['close'] < dataframe['bb_lowerband']) &
                # L'RSI è in iper-venduto
                (dataframe['rsi'] < 35) &
                # C'è un minimo di volume per garantire esecuzione
                (dataframe['volume'] > 0)
            ),
            'enter_long'] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                # Il prezzo tocca la banda superiore (picco raggiunto)
                (dataframe['close'] > dataframe['bb_upperband']) |
                # RSI troppo alto (iper-comprato)
                (dataframe['rsi'] > 75)
            ),
            'exit_long'] = 1

        return dataframe
