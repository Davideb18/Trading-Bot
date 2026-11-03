#!/usr/bin/env python3
"""
Exact De-Compounded (Fixed $5,000 Base) Multi-Year Prop Firm Analysis (2024-01-01 to 2026-08-16).
Reconstructs true non-compounded dollar/pct returns, 5m floating Daily DD, 5m Trailing DD,
inactivity gaps, Challenge pass times (1-Step & 2-Step), and Funded Account monthly payouts.
"""

import json
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[1]
FUT_DATA = ROOT / "user_data/data/binance/futures"
SPOT_DATA = ROOT / "user_data/data/binance"

_CACHE = {}


def load_5m_closes(pair: str, index: pd.DatetimeIndex) -> np.ndarray:
    key = (pair, index[0], index[-1])
    if key not in _CACHE:
        sym = pair.replace("/", "_").replace(":", "_")
        p_fut = FUT_DATA / f"{sym}-5m-futures.feather"
        if not p_fut.exists():
            sym_base = pair.split(":")[0].replace("/", "_")
            p_fut = FUT_DATA / f"{sym_base}_USDT-5m-futures.feather"
        p_spot = SPOT_DATA / f"{pair.split(':')[0].replace('/', '_')}-5m.feather"
        path = p_fut if p_fut.exists() else p_spot
        if path.exists():
            df = pd.read_feather(path, columns=["date", "close"])
            s = df.set_index("date")["close"].reindex(index).ffill().bfill()
            _CACHE[key] = s.to_numpy(dtype=float)
        else:
            _CACHE[key] = None
    return _CACHE[key]


def simulate_fixed_base_from_trades(
    trades_raw: list[dict],
    orig_initial_balance: float,
    orig_ratio: float,
    orig_slots: int,
    target_initial_balance: float,
    target_ratio: float,
    target_slots: int,
    start_date: str = "2024-01-01",
    end_date: str = "2026-08-16",
) -> dict:
    # Sort trades chronologically
    trades_sorted = sorted(trades_raw, key=lambda t: t["open_timestamp"])

    # Track the original compounded balance at each trade's open_timestamp so we know the exact
    # compounded slot stake that Freqtrade assigned to that trade, and replace it with Fixed Base!
    # Even more directly: in NFIX8, each trade's orders[0] ("first entry") was sized as a fraction
    # of the compounded wallet at open_timestamp.
    # Let's compute the exact compounded wallet balance right before each trade opened:
    closed_events = sorted(
        [(t["close_timestamp"], float(t["profit_abs"])) for t in trades_sorted],
        key=lambda x: x[0],
    )

    start_ts = int(pd.Timestamp(start_date, tz="UTC").timestamp() * 1000)
    end_ts = int(pd.Timestamp(end_date, tz="UTC").timestamp() * 1000)

    index = pd.date_range(start_date, end_date, freq="5min", tz="UTC")
    n = len(index)
    cash = np.full(n, target_initial_balance, dtype=float)
    pos_pnl = np.zeros(n, dtype=float)

    # Enforce target_slots concurrency limit cleanly
    active_close_times = []
    decomp_trades = []

    for t in trades_sorted:
        if t["open_timestamp"] < start_ts or t["close_timestamp"] > end_ts:
            continue

        # Remove closed trades before this trade's open_timestamp
        active_close_times = [ct for ct in active_close_times if ct > t["open_timestamp"]]
        if len(active_close_times) >= target_slots:
            # Slot full under target_slots
            continue
        active_close_times.append(t["close_timestamp"])

        # Original compounded wallet and per-slot budget at t["open_timestamp"]:
        realized_before = sum(p for ts, p in closed_events if ts <= t["open_timestamp"])
        orig_wallet_at_open = orig_initial_balance + realized_before
        orig_slot_budget_at_open = (orig_wallet_at_open * orig_ratio) / orig_slots

        # Target fixed (non-compounded) per-slot budget:
        target_slot_budget = (target_initial_balance * target_ratio) / target_slots

        # Scale factor removes 100% of compound interest AND scales per-slot stake to target_ratio / target_slots:
        scale = target_slot_budget / orig_slot_budget_at_open

        orders = sorted(t["orders"], key=lambda o: o["order_filled_timestamp"])
        closes = load_5m_closes(t["pair"], index)

        qty = 0.0
        avg_entry = 0.0
        last_idx = 0
        fee_open = float(t.get("fee_open", 0.00055))
        fee_close = float(t.get("fee_close", 0.00055))

        for o in orders:
            ts = pd.to_datetime(o["order_filled_timestamp"], unit="ms", utc=True)
            idx = min(max(int(index.searchsorted(ts, side="left")), 0), n - 1)
            if closes is not None and qty > 0 and idx > last_idx:
                pos_pnl[last_idx:idx] += qty * (closes[last_idx:idx] - avg_entry)

            amt = float(o["amount"]) * scale
            price = float(o["safe_price"])
            if o["ft_is_entry"]:
                fee = amt * price * fee_open
                avg_entry = (qty * avg_entry + amt * price) / (qty + amt) if (qty + amt) > 0 else price
                qty += amt
                cash[idx:] -= fee
            else:
                fee = amt * price * fee_close
                realized_step = amt * (price - avg_entry) - fee
                qty = max(0.0, qty - amt)
                cash[idx:] += realized_step
            last_idx = idx

        close_dt = pd.to_datetime(t["close_timestamp"], unit="ms", utc=True)
        close_idx = min(max(int(index.searchsorted(close_dt, side="left")), last_idx), n - 1)
        if closes is not None and qty > 0 and close_idx > last_idx:
            pos_pnl[last_idx:close_idx] += qty * (closes[last_idx:close_idx] - avg_entry)

        scaled_profit = float(t["profit_abs"]) * scale
        modeled_net = sum(
            (
                -float(o["amount"]) * scale * float(o["safe_price"]) * (1 + fee_open)
                if o["ft_is_entry"]
                else float(o["amount"]) * scale * float(o["safe_price"]) * (1 - fee_close)
            )
            for o in orders
        )
        cash[close_idx:] += (scaled_profit - modeled_net)

        first_stake_scaled = float(orders[0]["amount"]) * scale * float(orders[0]["safe_price"])
        max_stake_scaled = float(t.get("max_stake_amount", t["stake_amount"])) * scale
        n_entries = sum(1 for o in orders if o["ft_is_entry"])

        decomp_trades.append({
            "pair": t["pair"],
            "open_ts": t["open_timestamp"],
            "close_ts": t["close_timestamp"],
            "open_dt": pd.to_datetime(t["open_timestamp"], unit="ms", utc=True),
            "close_dt": close_dt,
            "first_stake_usdt": first_stake_scaled,
            "max_stake_usdt": max_stake_scaled,
            "profit_usdt": scaled_profit,
            "profit_ratio": float(t["profit_ratio"]),
            "n_entries": n_entries,
            "orders": [
                {
                    "ts": o["order_filled_timestamp"],
                    "is_entry": o["ft_is_entry"],
                    "notional": float(o["amount"]) * scale * float(o["safe_price"]),
                }
                for o in orders
            ],
            "exit_reason": t.get("exit_reason", ""),
        })

    equity = cash + pos_pnl
    eq_series = pd.Series(equity, index=index)

    # Compute Daily Drawdown & Trailing Drawdown (as % of fixed $5,000 initial balance)
    day_groups = eq_series.groupby(eq_series.index.date)
    day_open = day_groups.transform("first")
    day_peak = day_groups.cummax()

    dd_day_open_pct = float(((day_open - eq_series) / target_initial_balance * 100.0).max())
    dd_day_peak_pct = float(((day_peak - eq_series) / target_initial_balance * 100.0).max())
    max_trailing_dd_pct = float(((eq_series.cummax() - eq_series) / target_initial_balance * 100.0).max())

    df = pd.DataFrame(decomp_trades)
    df["year"] = df["close_dt"].dt.year
    df["ym"] = df["close_dt"].dt.strftime("%Y-%m")

    yearly = {}
    for yr in (2024, 2025, 2026):
        sub = df[df["year"] == yr]
        p_usdt = float(sub["profit_usdt"].sum()) if not sub.empty else 0.0
        yr_eq = eq_series.loc[f"{yr}-01-01":f"{yr}-12-31"]
        yr_do = yr_eq.groupby(yr_eq.index.date).transform("first")
        yr_dp = yr_eq.groupby(yr_eq.index.date).cummax()
        yearly[str(yr)] = {
            "trades": len(sub),
            "win_rate_pct": round(float((sub["profit_usdt"] > 0).mean() * 100.0), 2) if len(sub) else 0.0,
            "profit_usdt": round(p_usdt, 2),
            "profit_pct_on_5k": round(p_usdt / target_initial_balance * 100.0, 2),
            "max_daily_dd_open_pct": round(float(((yr_do - yr_eq) / target_initial_balance * 100.0).max()), 2),
            "max_daily_dd_peak_pct": round(float(((yr_dp - yr_eq) / target_initial_balance * 100.0).max()), 2),
            "max_trailing_dd_pct": round(float(((yr_eq.cummax() - yr_eq) / target_initial_balance * 100.0).max()), 2),
        }

    # Inactivity gaps
    fill_stamps = sorted(
        [start_ts, end_ts]
        + [o["ts"] for tr in decomp_trades for o in tr["orders"]]
    )
    gaps_days = np.diff(fill_stamps) / 86400000.0
    max_gap_days = float(gaps_days.max())
    gaps_over_30d = int((gaps_days >= 30.0).sum())

    # Rolling Challenge Pass Times (1-Step +10% = $500, 2-Step Step1 +10% = $500 + Step2 +5% = $250)
    daily_eq = eq_series.resample("1D").last()
    step1_days_list = []
    twostep_days_list = []
    for start_idx in range(0, len(daily_eq) - 60, 7):
        base_val = daily_eq.iloc[start_idx]
        s0_date = daily_eq.index[start_idx]
        future = daily_eq.iloc[start_idx:]
        hit_10 = future[future >= base_val + 500.0]
        if not hit_10.empty:
            d1_date = hit_10.index[0]
            s1_days = (d1_date - s0_date).days
            step1_days_list.append(s1_days)
            future2 = daily_eq.loc[d1_date:]
            base2 = future2.iloc[0]
            hit_5 = future2[future2 >= base2 + 250.0]
            if not hit_5.empty:
                d2_date = hit_5.index[0]
                twostep_days_list.append((d2_date - s0_date).days)

    total_profit = float(df["profit_usdt"].sum())
    months_count = 31.5  # Jan 2024 to mid-Aug 2026

    monthly = {}
    for ym, grp in df.groupby("ym"):
        pu = float(grp["profit_usdt"].sum())
        monthly[ym] = {
            "trades": len(grp),
            "wins": int((grp["profit_usdt"] > 0).sum()),
            "profit_usdt": round(pu, 2),
            "profit_pct_5k": round(pu / target_initial_balance * 100.0, 2),
            "funded_80pct_payout_usdt": round(max(0.0, pu * 0.80), 2),
        }

    return {
        "trades": len(df),
        "dca_trades": int((df["n_entries"] > 1).sum()),
        "win_rate_pct": round(float((df["profit_usdt"] > 0).mean() * 100.0), 2),
        "total_profit_usdt_non_compounded": round(total_profit, 2),
        "total_profit_pct_non_compounded": round(total_profit / target_initial_balance * 100.0, 2),
        "avg_monthly_profit_usdt": round(total_profit / months_count, 2),
        "avg_monthly_profit_pct": round((total_profit / months_count) / target_initial_balance * 100.0, 2),
        "avg_monthly_funded_80pct_usdt": round((total_profit / months_count) * 0.80, 2),
        "max_daily_dd_open_pct": round(dd_day_open_pct, 2),
        "max_daily_dd_peak_pct": round(dd_day_peak_pct, 2),
        "max_trailing_dd_pct": round(max_trailing_dd_pct, 2),
        "max_inactivity_gap_days": round(max_gap_days, 1),
        "inactivity_gaps_over_30d": gaps_over_30d,
        "median_days_1step_10pct": int(np.median(step1_days_list)) if step1_days_list else None,
        "median_days_2step_15pct": int(np.median(twostep_days_list)) if twostep_days_list else None,
        "yearly": yearly,
        "monthly": monthly,
    }


def main():
    # 1. Load 21-Pair Futures dataset (2023-2026)
    fut_path = ROOT / "user_data/backtest_results/latest_futures/backtest-result-2026-10-27_13-44-14.json"
    fut_data = json.loads(fut_path.read_text())
    fut_strat = next(iter(fut_data["strategy"]))
    fut_trades = fut_data["strategy"][fut_strat]["trades"]

    # 2. Load Top-40 Dynamic Volume Pairlist dataset (2021-2026)
    dyn_path = ROOT / "user_data/backtest_results/master_realistic_dynamic_5_years_trades.json"
    dyn_trades = json.loads(dyn_path.read_text())

    # 3. Curated Institutional Dynamic (45-pair Static Whitelist + Dynamic VolumePairList Top 35)
    curated_cfg = json.loads((ROOT / "user_data/config_prop_nfix8.json").read_text())
    curated_bases = {p.split("/")[0] for p in curated_cfg["exchange"]["pair_whitelist"]}
    curated_dyn_trades = [
        t for t in dyn_trades if t["pair"].split("/")[0] in curated_bases
    ]

    results = {}

    print("=== MULTI-YEAR NON-COMPOUNDED PROP FIRM MATRIX (2024-01-01 -> 2026-08-16, 31.5 MONTHS) ===\n")

    configs = [
        ("Static_21Pairs_R24_S3", fut_trades, 5000.0, 0.85, 6, 0.24, 3),
        ("Static_21Pairs_R24_S4", fut_trades, 5000.0, 0.85, 6, 0.24, 4),
        ("Static_21Pairs_R30_S3", fut_trades, 5000.0, 0.85, 6, 0.30, 3),
        ("Static_21Pairs_R36_S3", fut_trades, 5000.0, 0.85, 6, 0.36, 3),
        ("Dynamic_40Pairs_R24_S3", dyn_trades, 1000.0, 0.99, 6, 0.24, 3),
        ("Dynamic_40Pairs_R24_S4", dyn_trades, 1000.0, 0.99, 6, 0.24, 4),
        ("Dynamic_40Pairs_R30_S3", dyn_trades, 1000.0, 0.99, 6, 0.30, 3),
        ("Dynamic_40Pairs_R36_S3", dyn_trades, 1000.0, 0.99, 6, 0.36, 3),
        ("Hybrid_Curated35_R24_S3", curated_dyn_trades, 1000.0, 0.99, 6, 0.24, 3),
        ("Hybrid_Curated35_R24_S4", curated_dyn_trades, 1000.0, 0.99, 6, 0.24, 4),
        ("Hybrid_Curated35_R30_S3", curated_dyn_trades, 1000.0, 0.99, 6, 0.30, 3),
        ("Hybrid_Curated35_R30_S4", curated_dyn_trades, 1000.0, 0.99, 6, 0.30, 4),
    ]

    for label, tr_list, orig_bal, orig_r, orig_s, targ_r, targ_s in configs:
        res = simulate_fixed_base_from_trades(
            tr_list, orig_bal, orig_r, orig_s, 5000.0, targ_r, targ_s
        )
        results[label] = res
        print(
            f"[{label:22s}] Trades={res['trades']:3d} (DCA={res['dca_trades']:2d}) | WR={res['win_rate_pct']:5.2f}% | "
            f"Total Non-Comp={res['total_profit_usdt_non_compounded']:+8.2f}$ ({res['total_profit_pct_non_compounded']:+6.2f}%) | "
            f"2024={res['yearly']['2024']['profit_pct_on_5k']:+6.2f}% ({res['yearly']['2024']['profit_usdt']:+7.2f}$) | "
            f"2025={res['yearly']['2025']['profit_pct_on_5k']:+6.2f}% ({res['yearly']['2025']['profit_usdt']:+7.2f}$) | "
            f"2026={res['yearly']['2026']['profit_pct_on_5k']:+6.2f}% ({res['yearly']['2026']['profit_usdt']:+7.2f}$) | "
            f"Avg/Mo={res['avg_monthly_profit_usdt']:+6.2f}$ ({res['avg_monthly_profit_pct']:+5.2f}%) | "
            f"MaxDailyDD(Open/Peak)={res['max_daily_dd_open_pct']:4.2f}%/{res['max_daily_dd_peak_pct']:4.2f}% | "
            f"MaxTrailDD={res['max_trailing_dd_pct']:4.2f}% | "
            f"MaxGap={res['max_inactivity_gap_days']:4.1f}d (>{30}d: {res['inactivity_gaps_over_30d']}) | "
            f"1-Step(+10%)~{res['median_days_1step_10pct']}d | 2-Step(+15%)~{res['median_days_2step_15pct']}d"
        )

    out_path = ROOT / "docs/prop_multiyear_noncompounded_audit.json"
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nSaved full JSON audit to {out_path}")


if __name__ == "__main__":
    main()
