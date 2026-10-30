import re

filepath = '/home/ubuntu/FreqtradeBot/scripts/llm_risk_manager.py'
with open(filepath, 'r') as f:
    text = f.read()

# 1. Replace trigger_nuclear_option body
pattern = r'    def trigger_nuclear_option\(self, reason: str\):.*?(?=\n    def _check_drawdown)'
replacement = """    def trigger_nuclear_option(self, reason: str):
        logger.warning(
            f"⚠️ Nuclear Option check: {reason}. "
            f"Force-exit is DISABLED by user configuration. No positions will be closed."
        )
        return"""
text = re.sub(pattern, replacement + '\n', text, flags=re.DOTALL)

# 2. In _check_drawdown, replace the trigger
old_check = """        if drawdown > GLOBAL_HARD_STOPLOSS_PERCENT:
            self.trigger_nuclear_option(
                f"Global drawdown {drawdown_pct:.2f}% exceeded "
                f"threshold {GLOBAL_HARD_STOPLOSS_PERCENT * 100:.1f}%!"
            )
            return True"""

new_check = """        if drawdown > GLOBAL_HARD_STOPLOSS_PERCENT:
            logger.warning(
                f"⚠️ Global drawdown {drawdown_pct:.2f}% exceeded threshold. "
                f"Force-exit is DISABLED. Strategy manages its own positions."
            )
            return False"""

if old_check in text:
    text = text.replace(old_check, new_check)
    print("Drawdown check replaced successfully.")
else:
    print("Warning: old_check not matched directly, checking regex replacement...")
    old_check_re = r'        if drawdown > GLOBAL_HARD_STOPLOSS_PERCENT:.*?return True'
    text = re.sub(old_check_re, new_check.strip(), text, flags=re.DOTALL)

with open(filepath, 'w') as f:
    f.write(text)

print("Successfully patched llm_risk_manager.py on AWS EC2!")
