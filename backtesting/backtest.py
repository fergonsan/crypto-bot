# backtesting/backtest.py
from __future__ import annotations

import argparse
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import ccxt  # pip install ccxt
import numpy as np
import pandas as pd


# -----------------------------
# Helpers
# -----------------------------
def utc_ts(dt: pd.Timestamp) -> int:
    if dt.tzinfo is None:
        dt = dt.tz_localize("UTC")
    return int(dt.timestamp() * 1000)


def parse_utc_date(s: str) -> pd.Timestamp:
    # accepts YYYY-MM-DD
    return pd.Timestamp(s, tz="UTC")


def safe_mkdir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def to_float(x) -> float:
    try:
        return float(x)
    except Exception:
        return float("nan")


def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n, min_periods=n).mean()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def donchian_high(df: pd.DataFrame, n: int) -> pd.Series:
    # highest high of previous n bars (exclude current)
    return df["high"].shift(1).rolling(n, min_periods=n).max()


def donchian_low(df: pd.DataFrame, n: int) -> pd.Series:
    # lowest low of previous n bars (exclude current)
    return df["low"].shift(1).rolling(n, min_periods=n).min()


# -----------------------------
# Data fetch
# -----------------------------
def make_exchange() -> ccxt.Exchange:
    ex = ccxt.binance(
        {
            "enableRateLimit": True,
            "options": {
                "defaultType": "spot",
            },
        }
    )
    return ex


def fetch_ohlcv_full(
    ex: ccxt.Exchange,
    symbol: str,
    timeframe: str,
    start: pd.Timestamp,
    end: Optional[pd.Timestamp] = None,
    limit: int = 1000,
    sleep_s: float = 0.15,
) -> pd.DataFrame:
    """
    Fetch OHLCV from Binance spot with pagination.
    Returns dataframe indexed by UTC timestamp.
    """
    since = utc_ts(start)
    end_ms = utc_ts(end) if end is not None else None

    all_rows: List[List[float]] = []
    last_since = None

    while True:
        if last_since is not None and since <= last_since:
            # safety guard against infinite loop
            since = last_since + 1

        rows = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=limit)
        if not rows:
            break

        all_rows.extend(rows)
        last_ts = rows[-1][0]
        last_since = since

        # advance since to last_ts + 1ms
        since = last_ts + 1

        if end_ms is not None and last_ts >= end_ms:
            break

        # rate-limit friendliness
        time.sleep(sleep_s)

        # another guard: if we got fewer than limit, likely end of data
        if len(rows) < limit:
            break

    if not all_rows:
        raise RuntimeError(f"No OHLCV fetched for {symbol}")

    df = pd.DataFrame(
        all_rows, columns=["time", "open", "high", "low", "close", "volume"]
    )
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    df = df.drop_duplicates(subset=["time"]).sort_values("time").set_index("time")

    if end is not None:
        df = df.loc[:end]

    # ensure numeric
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)

    return df


# -----------------------------
# Backtest core
# -----------------------------
@dataclass
class Position:
    symbol: str
    qty: float
    entry_price: float
    entry_time: pd.Timestamp
    entry_atr: float
    hard_stop: float
    trail_stop: float


def compute_fee_and_slip(notional: float, fee_rate: float, slip_rate: float) -> Tuple[float, float]:
    fee = notional * fee_rate
    slip = notional * slip_rate
    return fee, slip


def run_backtest(
    data: Dict[str, pd.DataFrame],
    initial_equity: float,
    donch_entry: int,
    donch_exit: int,
    atr_n: int,
    sma_fast: int,
    sma_slow: int,
    trail_atr_mult: float,
    hard_stop_atr_mult: float,
    risk_per_trade: float,
    stop_on_low: bool,
    fee_rate: float,
    slip_rate: float,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, Dict[str, float]]:
    """
    Multi-asset, long-only, shared equity.
    Conservative fills:
      - Entry on close breakout (filled at close)
      - Exits:
          - stop_on_low: if low <= stop -> fill at max(open, stop) for gap-down realism
          - else: evaluate on close (filled at close)
    """
    # align on common timeline (union of timestamps)
    all_times = sorted(set().union(*[df.index for df in data.values()]))

    # precompute indicators per symbol
    ind: Dict[str, pd.DataFrame] = {}
    for sym, df in data.items():
        d = df.copy()
        d["atr"] = atr(d, atr_n)
        d["donch_high"] = donchian_high(d, donch_entry)
        d["donch_low"] = donchian_low(d, donch_exit)
        d["sma_fast"] = sma(d["close"], sma_fast)
        d["sma_slow"] = sma(d["close"], sma_slow)
        d["regime_on"] = d["sma_fast"] > d["sma_slow"]
        ind[sym] = d

    equity = initial_equity
    cash = initial_equity
    positions: Dict[str, Position] = {}

    equity_curve_rows = []
    trades_rows = []
    roundtrips_rows = []

    # for roundtrip pairing
    open_trade_meta: Dict[str, Dict] = {}

    def mark_to_market(ts: pd.Timestamp) -> float:
        total = cash
        for sym, pos in positions.items():
            if ts in ind[sym].index:
                px = float(ind[sym].loc[ts, "close"])
            else:
                # if missing candle, skip valuation (rare)
                px = pos.entry_price
            total += pos.qty * px
        return total

    for ts in all_times:
        # first: manage exits
        for sym in list(positions.keys()):
            d = ind[sym]
            if ts not in d.index:
                continue

            row = d.loc[ts]
            o, h, l, c = map(to_float, [row["open"], row["high"], row["low"], row["close"]])
            a = to_float(row["atr"])
            regime_on = bool(row["regime_on"])
            donch_low_v = to_float(row["donch_low"])

            pos = positions[sym]

            # update trailing stop (only if ATR available)
            if not math.isnan(a):
                new_trail = c - trail_atr_mult * a
                pos.trail_stop = max(pos.trail_stop, new_trail)

            stop_price = max(pos.hard_stop, pos.trail_stop)

            exit_reason = None
            fill_price = None

            if stop_on_low:
                # Intrabar stop: if low breaches, we exit at stop (or open if gapped below)
                if l <= stop_price:
                    fill_price = max(o, stop_price)
                    exit_reason = "exit_stop_intrabar"
            else:
                # Close-based stop
                if c <= stop_price:
                    fill_price = c
                    exit_reason = "exit_stop_close"

            # Donchian / regime exits (evaluated on close)
            if exit_reason is None:
                if (not math.isnan(donch_low_v)) and (c < donch_low_v):
                    fill_price = c
                    exit_reason = "exit_donch"
                elif not regime_on:
                    fill_price = c
                    exit_reason = "exit_regime_off"

            if exit_reason is None or fill_price is None:
                continue

            # execute exit
            notional = pos.qty * fill_price
            fee, slip = compute_fee_and_slip(notional, fee_rate, slip_rate)
            cash += notional - fee - slip

            trades_rows.append(
                {
                    "symbol": sym,
                    "side": "sell",
                    "time": ts.isoformat(),
                    "price": fill_price,
                    "qty": pos.qty,
                    "notional": notional,
                    "fee": fee,
                    "slippage_cost": slip,
                    "reason": exit_reason,
                }
            )

            # roundtrip
            meta = open_trade_meta.get(sym, {})
            entry_notional = meta.get("entry_notional", pos.qty * pos.entry_price)
            entry_fee = meta.get("entry_fee", 0.0)
            entry_slip = meta.get("entry_slip", 0.0)
            entry_time = meta.get("entry_time", pos.entry_time)

            pnl_gross = (fill_price - pos.entry_price) * pos.qty
            pnl_net = pnl_gross - (entry_fee + entry_slip + fee + slip)

            roundtrips_rows.append(
                {
                    "symbol": sym,
                    "entry_time": entry_time.isoformat(),
                    "exit_time": ts.isoformat(),
                    "entry_price": pos.entry_price,
                    "exit_price": fill_price,
                    "qty": pos.qty,
                    "pnl_gross": pnl_gross,
                    "pnl_net": pnl_net,
                    "fees": entry_fee + fee,
                    "slippage_cost": entry_slip + slip,
                    "reason_exit": exit_reason,
                    "hold_hours": (ts - entry_time).total_seconds() / 3600.0,
                }
            )

            positions.pop(sym, None)
            open_trade_meta.pop(sym, None)

        # second: entries
        for sym, d in ind.items():
            if sym in positions:
                continue
            if ts not in d.index:
                continue

            row = d.loc[ts]
            c = to_float(row["close"])
            a = to_float(row["atr"])
            donch_high_v = to_float(row["donch_high"])
            regime_on = bool(row["regime_on"])

            if not regime_on:
                continue
            if math.isnan(donch_high_v) or math.isnan(a):
                continue

            # breakout on close
            if c <= donch_high_v:
                continue

            # position sizing by risk_per_trade using stop distance
            # hard stop anchored at entry using entry ATR
            hard_stop = c - hard_stop_atr_mult * a
            trail_stop = c - trail_atr_mult * a
            stop_price = max(hard_stop, trail_stop)  # initial effective stop

            stop_dist = c - stop_price
            if stop_dist <= 0:
                continue

            risk_budget = risk_per_trade * mark_to_market(ts)
            qty = risk_budget / stop_dist

            # also cap by available cash (spot)
            max_qty_cash = cash / c if c > 0 else 0.0
            qty = min(qty, max_qty_cash)

            # ignore dust
            if qty <= 0 or qty * c < 10:
                continue

            notional = qty * c
            fee, slip = compute_fee_and_slip(notional, fee_rate, slip_rate)
            total_cost = notional + fee + slip
            if total_cost > cash:
                # adjust qty down
                qty = cash / (c * (1 + fee_rate + slip_rate))
                notional = qty * c
                fee, slip = compute_fee_and_slip(notional, fee_rate, slip_rate)
                total_cost = notional + fee + slip

            if qty <= 0 or total_cost > cash:
                continue

            cash -= total_cost
            pos = Position(
                symbol=sym,
                qty=qty,
                entry_price=c,
                entry_time=ts,
                entry_atr=a,
                hard_stop=hard_stop,
                trail_stop=trail_stop,
            )
            positions[sym] = pos
            open_trade_meta[sym] = {
                "entry_time": ts,
                "entry_notional": notional,
                "entry_fee": fee,
                "entry_slip": slip,
            }

            trades_rows.append(
                {
                    "symbol": sym,
                    "side": "buy",
                    "time": ts.isoformat(),
                    "price": c,
                    "qty": qty,
                    "notional": notional,
                    "fee": fee,
                    "slippage_cost": slip,
                    "reason": "entry_donch_breakout_regime_on",
                }
            )

        # record equity curve
        eq = mark_to_market(ts)
        equity_curve_rows.append(
            {
                "time": ts.isoformat(),
                "equity": eq,
                "cash": cash,
                "positions_value": eq - cash,
                "num_positions": len(positions),
            }
        )

    equity_curve = pd.DataFrame(equity_curve_rows)
    trades_df = pd.DataFrame(trades_rows)
    roundtrips_df = pd.DataFrame(roundtrips_rows)

    # stats
    start_eq = initial_equity
    end_eq = float(equity_curve["equity"].iloc[-1]) if len(equity_curve) else initial_equity
    total_return = (end_eq / start_eq - 1.0) if start_eq > 0 else 0.0

    # max drawdown
    eq_series = equity_curve["equity"].astype(float)
    peak = eq_series.cummax()
    dd = (eq_series / peak - 1.0)
    max_dd = float(dd.min()) if len(dd) else 0.0

    # annualized return based on days
    if len(equity_curve) >= 2:
        t0 = pd.to_datetime(equity_curve["time"].iloc[0])
        t1 = pd.to_datetime(equity_curve["time"].iloc[-1])
        days = max(1.0, (t1 - t0).total_seconds() / 86400.0)
        years = days / 365.25
        ann_return = (end_eq / start_eq) ** (1.0 / years) - 1.0 if years > 0 else 0.0
    else:
        ann_return = 0.0

    # trade stats from roundtrips
    if len(roundtrips_df):
        wins = (roundtrips_df["pnl_net"] > 0).sum()
        losses = (roundtrips_df["pnl_net"] <= 0).sum()
        winrate = wins / len(roundtrips_df)
        gross_profit = roundtrips_df.loc[roundtrips_df["pnl_net"] > 0, "pnl_net"].sum()
        gross_loss = -roundtrips_df.loc[roundtrips_df["pnl_net"] <= 0, "pnl_net"].sum()
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
        avg_trade = roundtrips_df["pnl_net"].mean()
        avg_win = roundtrips_df.loc[roundtrips_df["pnl_net"] > 0, "pnl_net"].mean()
        avg_loss = roundtrips_df.loc[roundtrips_df["pnl_net"] <= 0, "pnl_net"].mean()
        avg_hold_h = roundtrips_df["hold_hours"].mean()
        net_pnl = roundtrips_df["pnl_net"].sum()
    else:
        winrate = 0.0
        profit_factor = 0.0
        avg_trade = 0.0
        avg_win = 0.0
        avg_loss = 0.0
        avg_hold_h = 0.0
        net_pnl = 0.0

    stats = {
        "start_equity": start_eq,
        "end_equity": end_eq,
        "total_return": total_return,
        "ann_return": ann_return,
        "max_dd": max_dd,
        "roundtrips": float(len(roundtrips_df)),
        "winrate": float(winrate),
        "net_pnl": float(net_pnl),
        "avg_trade": float(avg_trade),
        "avg_win": float(0.0 if math.isnan(avg_win) else avg_win),
        "avg_loss": float(0.0 if math.isnan(avg_loss) else avg_loss),
        "profit_factor": float(profit_factor),
        "avg_hold_h": float(0.0 if math.isnan(avg_hold_h) else avg_hold_h),
    }

    return equity_curve, trades_df, roundtrips_df, stats


# -----------------------------
# CLI / main
# -----------------------------
def fmt_pct(x: float) -> str:
    return f"{x*100:.2f}%"


def fmt_money(x: float) -> str:
    return f"{x:,.2f}"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", required=True, help="Comma-separated, e.g. BTC/USDC,ETH/USDC")
    p.add_argument("--timeframe", default="1d")
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", default=None, help="YYYY-MM-DD (optional)")
    p.add_argument("--initial", type=float, default=1000.0)

    p.add_argument("--donch_entry", type=int, default=30)
    p.add_argument("--donch_exit", type=int, default=20)

    p.add_argument("--atr_n", type=int, default=14)
    p.add_argument("--sma_fast", type=int, default=50)
    p.add_argument("--sma_slow", type=int, default=200)
    p.add_argument("--trail_atr_mult", type=float, default=3.0)
    p.add_argument("--hard_stop_atr_mult", type=float, default=1.5)

    p.add_argument("--risk_per_trade", type=float, default=0.02, help="e.g. 0.02 = 2% equity risk")
    p.add_argument("--stop_on_low", action="store_true", help="Conservative intrabar stop execution")

    p.add_argument("--fee_rate", type=float, default=0.0, help="e.g. 0.001 = 0.10%")
    p.add_argument("--slip_rate", type=float, default=0.0, help="e.g. 0.0005 = 0.05%")

    p.add_argument("--outdir", default="backtest_out")
    args = p.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    start = parse_utc_date(args.start)
    end = parse_utc_date(args.end) if args.end else None

    ex = make_exchange()
    ex.load_markets()

    data: Dict[str, pd.DataFrame] = {}
    for sym in symbols:
        if sym not in ex.markets:
            raise RuntimeError(
                f"Symbol {sym} not found on Binance spot via ccxt. "
                f"Check exact name in ex.markets (maybe different quote)."
            )
        df = fetch_ohlcv_full(ex, sym, args.timeframe, start=start, end=end)
        data[sym] = df

    equity_curve, trades_df, roundtrips_df, stats = run_backtest(
        data=data,
        initial_equity=args.initial,
        donch_entry=args.donch_entry,
        donch_exit=args.donch_exit,
        atr_n=args.atr_n,
        sma_fast=args.sma_fast,
        sma_slow=args.sma_slow,
        trail_atr_mult=args.trail_atr_mult,
        hard_stop_atr_mult=args.hard_stop_atr_mult,
        risk_per_trade=args.risk_per_trade,
        stop_on_low=args.stop_on_low,
        fee_rate=args.fee_rate,
        slip_rate=args.slip_rate,
    )

    safe_mkdir(args.outdir)
    equity_curve.to_csv(os.path.join(args.outdir, "equity_curve.csv"), index=False)
    trades_df.to_csv(os.path.join(args.outdir, "trades.csv"), index=False)
    roundtrips_df.to_csv(os.path.join(args.outdir, "roundtrips.csv"), index=False)

    # Period display
    t0 = pd.to_datetime(equity_curve["time"].iloc[0]) if len(equity_curve) else start
    t1 = pd.to_datetime(equity_curve["time"].iloc[-1]) if len(equity_curve) else (end or start)
    days = max(1.0, (t1 - t0).total_seconds() / 86400.0)
    years = days / 365.25

    print("\n===== BACKTEST SUMMARY =====")
    print(f"Period      : {t0.date()} \u2192 {t1.date()}  ({int(days)} days, {years:.2f} years)")
    print(f"Start equity: {fmt_money(stats['start_equity'])} USDC")
    print(f"End equity  : {fmt_money(stats['end_equity'])} USDC")
    print(f"Total return: {fmt_pct(stats['total_return'])}")
    print(f"Ann. return : {fmt_pct(stats['ann_return'])}")
    print(f"Max DD      : {fmt_pct(stats['max_dd'])}  ({fmt_money(stats['end_equity'] * stats['max_dd'])} USDC)")

    print("\n--- Trades/Performance ---")
    print(f"Round-trips : {int(stats['roundtrips'])}")
    print(f"Winrate     : {stats['winrate']*100:.1f}%")
    print(f"Net PnL     : {fmt_money(stats['net_pnl'])} USDC")
    print(f"Avg/trade   : {fmt_money(stats['avg_trade'])} USDC")
    print(f"Avg win     : {fmt_money(stats['avg_win'])} USDC")
    print(f"Avg loss    : {fmt_money(stats['avg_loss'])} USDC")
    print(f"ProfitFactor: {stats['profit_factor']:.2f}")
    print(f"Expectancy  : {fmt_money(stats['avg_trade'])} USDC")
    print(f"Avg hold    : {stats['avg_hold_h']:.1f} h")

    print("\nSaved:")
    print(f" - {os.path.join(args.outdir, 'equity_curve.csv')}")
    print(f" - {os.path.join(args.outdir, 'trades.csv')}")
    print(f" - {os.path.join(args.outdir, 'roundtrips.csv')}")


if __name__ == "__main__":
    main()