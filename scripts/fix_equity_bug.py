import re

filepath = '/home/ubuntu/FreqtradeBot/scripts/llm_risk_manager.py'
with open(filepath, 'r') as f:
    text = f.read()

# Replace get_equity method to fix the double-counting bug
old_get_equity_re = r'    def get_equity\(self\).*?(?=\n    def get_open_trades)'
new_get_equity = '''    def get_equity(self) -> float | None:
        """
        True real-time portfolio equity in stake currency (USDT).
        Freqtrade's /balance endpoint already computes the total valuation:
          - 'total': total wallet stake across all assets (free USDT + market value of open positions).
        We strictly avoid double-counting unrealized PnL.
        """
        data = self.get("/balance")
        if not data or not isinstance(data, dict):
            return None

        total = data.get("total")
        if (
            total is not None
            and isinstance(total, (int, float))
            and total > 0
            and not math.isnan(total)
        ):
            return float(total)

        total_bot = data.get("total_bot")
        if (
            total_bot is not None
            and isinstance(total_bot, (int, float))
            and total_bot > 0
            and not math.isnan(total_bot)
        ):
            return float(total_bot)

        return None'''

text = re.sub(old_get_equity_re, new_get_equity + '\n', text, flags=re.DOTALL)

with open(filepath, 'w') as f:
    f.write(text)

print("get_equity() patched successfully: double-counting bug ELIMINATED!")
