import os
import time
import requests
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("LLM_Risk_Manager")

# --- Configuration ---
FREQTRADE_API_URL = "http://127.0.0.1:8080/api/v1"
FREQTRADE_JWT_TOKEN = os.getenv("FREQTRADE_JWT_TOKEN", "supersecretjwtkeysupersecretjwtkeysupersecretjwtkey")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Risk Limits
GLOBAL_HARD_STOPLOSS_PERCENT = 0.05 # Max 5% drawdown globally before nuclear option

class RiskManager:
    def __init__(self):
        self.headers = {
            "Authorization": f"Bearer {FREQTRADE_JWT_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        self.initial_balance = None
        self.nuclear_triggered = False

    def get_account_balance(self):
        """Fetches the current wallet balance from Freqtrade."""
        try:
            response = requests.get(f"{FREQTRADE_API_URL}/balance", headers=self.headers, timeout=5)
            if response.status_code == 200:
                data = response.json()
                total_balance = data.get("total", 0.0)
                
                # Set initial balance on first successful run
                if self.initial_balance is None and total_balance > 0:
                    self.initial_balance = total_balance
                    logger.info(f"Initial balance locked at: {self.initial_balance} USDT")
                    
                return total_balance
            return None
        except Exception as e:
            logger.error(f"Failed to fetch balance: {e}")
            return None

    def trigger_nuclear_option(self, reason):
        """Emergency stop: Force exit all trades and stop buying."""
        if self.nuclear_triggered:
            return
            
        logger.critical(f"☢️ NUCLEAR OPTION TRIGGERED! Reason: {reason}")
        self.nuclear_triggered = True
        
        try:
            # 1. Stop new buys immediately
            requests.post(f"{FREQTRADE_API_URL}/stopbuy", headers=self.headers, timeout=5)
            logger.info("Bot buying halted.")
            
            # 2. Force exit all open trades
            # Fetch all open trades first
            status_res = requests.get(f"{FREQTRADE_API_URL}/status", headers=self.headers, timeout=5)
            if status_res.status_code == 200:
                trades = status_res.json()
                for trade in trades:
                    trade_id = trade.get('trade_id')
                    logger.warning(f"Force exiting trade ID: {trade_id}")
                    requests.post(f"{FREQTRADE_API_URL}/forceexit", headers=self.headers, json={"tradeid": trade_id}, timeout=5)
            
            logger.critical("All funds moved to safety. Risk Manager entering hibernation.")
        except Exception as e:
            logger.error(f"Failed to execute nuclear option: {e}")

    def analyze_market_regime(self):
        """
        Uses LLM / Sentiment API to determine the current market regime.
        Returns: 'bull' (normal), 'bear' (reduce risk), or 'panic' (nuclear)
        """
        # Placeholder: Here you would call Gemini API with recent news headlines.
        # For safety default, we assume normal market unless overridden.
        regime = "bull" 
        return regime

    def run(self):
        logger.info("Starting LLM Risk Manager...")
        while not self.nuclear_triggered:
            try:
                # 1. Hard-Stop Check (Mathematical Safety)
                current_balance = self.get_account_balance()
                if current_balance and self.initial_balance:
                    drawdown = (self.initial_balance - current_balance) / self.initial_balance
                    if drawdown > GLOBAL_HARD_STOPLOSS_PERCENT:
                        self.trigger_nuclear_option(f"Global Drawdown exceeded {GLOBAL_HARD_STOPLOSS_PERCENT * 100}%!")
                        continue

                # 2. AI Sentinel Check (Macro Events)
                regime = self.analyze_market_regime()
                if regime == "panic":
                    self.trigger_nuclear_option("LLM detected extreme Black Swan panic event.")
                    continue
                elif regime == "bear":
                    logger.info("Market is nervous. (Here we would dynamically reduce max_open_trades via API if supported).")

                # Sleep until next check
                time.sleep(60) # Check every 60 seconds
                
            except Exception as e:
                logger.error(f"Risk Manager loop error: {e}")
                time.sleep(60)

if __name__ == "__main__":
    # manager = RiskManager()
    # manager.run()
    pass
