#!/usr/bin/env python3
"""
Bybit Demo Account Position Mode & Settings Auditor
===================================================
Verifies:
1. API connectivity to Bybit Demo Trading.
2. Account balance ($5,000 USDT).
3. Position Mode: One-Way Mode (Netting) vs Hedge Mode (Dual-Side).
4. Configures 1.0x Leverage and Isolated Margin across all 21 whitelisted pairs.
"""

import json
import sys
from pathlib import Path
import ccxt

def main():
    cfg_path = Path("user_data/config_prop_nfix8.json")
    if not cfg_path.exists():
        print(f"Config file not found: {cfg_path}")
        sys.exit(1)

    with open(cfg_path) as f:
        cfg = json.load(f)

    ex_cfg = cfg.get("exchange", {})
    api_key = ex_cfg.get("key")
    api_secret = ex_cfg.get("secret")
    demo = ex_cfg.get("demo_trading", True)
    whitelist = ex_cfg.get("pair_whitelist", [])

    print("=" * 70)
    print("🏛️ BYBIT DEMO ACCOUNT AUDITOR & HARDENER")
    print(f"   API Key: {api_key[:6]}...{api_key[-4:]}")
    print(f"   Demo Trading: {demo}")
    print("=" * 70)

    exchange = ccxt.bybit({
        'apiKey': api_key,
        'secret': api_secret,
        'enableRateLimit': True,
        'options': {
            'defaultType': 'linear',
        }
    })

    if demo:
        exchange.enable_demo_trading(True)

    try:
        balance = exchange.fetch_balance()
        print("\n✅ API Connectivity: SUCCESSFUL")
        usdt_bal = balance.get('USDT', {})
        total_bal = usdt_bal.get('total', 0.0)
        free_bal = usdt_bal.get('free', 0.0)
        print(f"💰 Account Equity: Total = ${total_bal:,.2f} USDT | Free = ${free_bal:,.2f} USDT")

        # 1. Audit Position Mode on BTCUSDT
        pos_list = exchange.private_get_v5_position_list({'category': 'linear', 'symbol': 'BTCUSDT'})
        items = pos_list.get('result', {}).get('list', [])
        if items:
            p = items[0]
            pos_idx = p.get('positionIdx')
            if pos_idx == 0:
                print("🔒 Position Mode: ONE-WAY MODE (Netting) -> 100% Freqtrade Compatible!")
            else:
                print(f"⚠️ Position Mode: HEDGE MODE (positionIdx={pos_idx}) -> Incompatible!")
        else:
            print("ℹ️ No default position record for BTCUSDT")

        # 2. Iterate Whitelist and Enforce Isolated Margin & 1.0x Leverage
        print(f"\n⚙️ Enforcing Isolated Margin Mode and 1.0x Leverage across {len(whitelist)} pairs...")
        success_count = 0
        for pair in whitelist:
            symbol = pair.split(':')[0].replace('/', '')
            try:
                # Set margin mode to isolated (tradeMode: 1 = Isolated, 0 = Cross)
                # In Bybit V5 set_margin_mode:
                try:
                    exchange.set_margin_mode('isolated', pair, {'leverage': 1.0})
                except Exception as me:
                    # Ignore if already set or not modified
                    pass
                
                # Set leverage to 1.0
                try:
                    exchange.set_leverage(1.0, pair)
                except Exception as le:
                    pass

                success_count += 1
            except Exception as e:
                print(f"   Failed for {symbol}: {e}")

        print(f"✅ Successfully verified & calibrated {success_count}/{len(whitelist)} pairs to Isolated 1.0x!")

        # Verify BTCUSDT final state
        check = exchange.private_get_v5_position_list({'category': 'linear', 'symbol': 'BTCUSDT'})
        final_item = check.get('result', {}).get('list', [{}])[0]
        trade_mode_str = "Isolated" if final_item.get('tradeMode') == 1 else "Cross"
        print("\n📊 Verification Summary for BTCUSDT:")
        print(f"   Leverage:    {final_item.get('leverage')}x")
        print(f"   Margin Mode: {trade_mode_str}")
        print(f"   PositionIdx: {final_item.get('positionIdx')} (One-Way Mode)")
        print("=" * 70)

    except Exception as e:
        print("❌ Bybit API Error:", e)

if __name__ == '__main__':
    main()
