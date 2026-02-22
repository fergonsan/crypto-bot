import os
import argparse
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional

import numpy as np
import pandas as pd
import ccxt


# -----------------------------
# Indicators
# -----------------------------
def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            (df["high"] - df["low"]),
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    return true_range(df).rolling(n).mean()


def slope(series: pd.Series, n: int) -> pd.Series:
    """
    Pendiente simple: (x - x.shift(n)) / n
    """
    return (series - series.shift(n)) / float(n)


def compute_indicators(
    df: pd.DataFrame,
    sma_fast: int,
    sma_slow: int,
    donch_entry: int,
    donch_exit: int,
    atr_len: int,
    slope_len: int,
) -> pd.DataFrame:
    df = df.copy()
    df["sma_fast"] = sma(df["close"], sma_fast)
    df["sma_slow"] = sma(df["close"], sma_slow)
    df["sma_slow_slope"] = slope(df["sma_slow"], slope_len)
    df[f"donch_high_{donch_entry}"] = df["high"].shift(1).rolling(donch_entry).max()
    df[f"donch_low_{donch_exit}"] = df["low"].shift(1).rolling(donch_exit).min()
    df[f"atr_{atr_len}"] = atr(df, atr_len)
    return df


# -----------------------------
# Backtest structs
# -----------------------------
@dataclass
class Trade:
    symbol: str
    side: str  # buy/sell
    time: pd.Timestamp
    price: float
    qty: float
    notional: float
    fee: float
    slippage_cost: float
    reason: str


@dataclass
class Position:
    qty: float = 0.0
    avg_price: float = 0.0
    trail_stop: float = 0.0  # trailing stop price (0 => inactive)


# -----------------------------
# Execution modeling
# -----------------------------
def apply_slippage(price: float, side: str, slippage_bps: float) -> float:
    s = slippage_bps / 10_000.0
    if side == "buy":
        return price * (1.0 + s)
    return price * (1.0 - s)


def fee_amount(notional: float, fee_bps: float) -> float:
    return notional * (fee_bps / 10_000.0)


# -----------------------------
# Risk / sizing
# -----------------------------
def position_size_qty(
    equity: float,
    risk_pct: float,
    atr_val: float,
    close: float,
    stop_atr_mult: float,
    max_order_notional: float,
    max_exposure_pct: float,
) -> float:
    """
    Sizing estilo bot:
      riesgo $ = equity * risk_pct
      stop distance = stop_atr_mult * ATR
      qty = riesgo/stop_distance
    caps:
      notional <= max_order_notional
      exposure <= equity*max_exposure_pct
    """
    if equity <= 0 or close <= 0 or atr_val <= 0:
        return 0.0

    risk_dollars = equity * risk_pct
    stop_dist = stop_atr_mult * atr_val
    if stop_dist <= 0:
        return 0.0

    qty = risk_dollars / stop_dist

    qty = min(qty, max_order_notional / close)
    qty = min(qty, (equity * max_exposure_pct) / close)

    return max(qty, 0.0)


# -----------------------------
# Metrics
# -----------------------------
def max_drawdown(equity_curve: pd.Series) -> float:
    eq = pd.to_numeric(equity_curve, errors="coerce").astype(float)
    peak = eq.cummax()
    dd = (eq / peak) - 1.0
    return float(dd.min()) if len(dd) else 0.0


def max_drawdown_abs(equity_curve: pd.Series) -> float:
    eq = pd.to_numeric(equity_curve, errors="coerce").astype(float)
    peak = eq.cummax()
    dd_abs = eq - peak  # negativo
    return float(dd_abs.min()) if len(dd_abs) else 0.0


def annualized_return(start_eq: float, end_eq: float, years: float) -> float:
    if start_eq <= 0 or years <= 0:
        return 0.0
    return (end_eq / start_eq) ** (1.0 / years) - 1.0


# -----------------------------
# Data fetch
# -----------------------------
def fetch_ohlcv_df(ex, symbol: str, timeframe: str, since_ms: int, limit: int = 1000) -> pd.DataFrame:
    all_rows = []
    ms = since_ms
    while True:
        chunk = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=ms, limit=limit)
        if not chunk:
            break
        all_rows.extend(chunk)
        last_ts = chunk[-1][0]
        ms = last_ts + 1
        if len(chunk) < limit:
            break

    df = pd.DataFrame(all_rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["time"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.drop(columns=["ts"])
    df = df.drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)
    return df


# -----------------------------
# Regime
# -----------------------------
def is_regime_on(row: pd.Series, regime: str) -> bool:
    close = float(row["close"])
    sma_slow = row["sma_slow"]
    sma_fast = row["sma_fast"]
    slow_slope = row["sma_slow_slope"]

    if pd.isna(sma_slow):
        return False

    sma_slow = float(sma_slow)

    if regime == "sma200":
        return close > sma_slow

    if regime == "sma50_gt_sma200":
        if pd.isna(sma_fast):
            return False
        return (close > sma_slow) and (float(sma_fast) > sma_slow)

    if regime == "sma200_slope_pos":
        if pd.isna(slow_slope):
            return False
        return (close > sma_slow) and (float(slow_slope) > 0.0)

    raise ValueError(f"Unknown regime: {regime}")


# -----------------------------
# Backtest
# -----------------------------
def run_backtest(
    symbols: List[str],
    timeframe: str,
    start_date: str,
    initial_equity: float,
    # strategy params
    regime: str,
    sma_fast: int,
    sma_slow: int,
    slope_len: int,
    donch_entry: int,
    donch_exit: int,
    atr_len: int,
    trail_atr_mult: float,  # 0 disables trailing
    # risk/execution
    risk_pct: float,
    stop_atr_mult: float,
    fee_bps: float,
    slippage_bps: float,
    max_order_notional: float,
    max_exposure_pct: float,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    ex = ccxt.binance({"enableRateLimit": True})
    ex.load_markets()

    start_dt = pd.Timestamp(start_date, tz="UTC")
    since_ms = int(start_dt.timestamp() * 1000)

    data: Dict[str, pd.DataFrame] = {}
    for sym in symbols:
        df = fetch_ohlcv_df(ex, sym, timeframe, since_ms)
        if len(df) < (sma_slow + 60):
            raise RuntimeError(f"Pocos datos para {sym}: {len(df)} filas. Ajusta start_date o timeframe.")
        df = compute_indicators(
            df=df,
            sma_fast=sma_fast,
            sma_slow=sma_slow,
            donch_entry=donch_entry,
            donch_exit=donch_exit,
            atr_len=atr_len,
            slope_len=slope_len,
        )
        data[sym] = df

    cash = float(initial_equity)
    positions: Dict[str, Position] = {sym: Position() for sym in symbols}
    trades: List[Trade] = []

    # timeline unión de timestamps
    base_t = data[symbols[0]][["time"]].copy()
    for sym in symbols[1:]:
        base_t = base_t.merge(data[sym][["time"]], on="time", how="outer")

    timeline = base_t.sort_values("time")["time"].dropna().reset_index(drop=True)

    def last_row(sym: str, now: pd.Timestamp) -> Optional[pd.Series]:
        df = data[sym]
        i = df["time"].searchsorted(now, side="right") - 1
        if i < 0:
            return None
        return df.loc[i]

    def mark_to_market(now: pd.Timestamp) -> float:
        eq = cash
        for sym in symbols:
            pos = positions[sym]
            if pos.qty > 0:
                r = last_row(sym, now)
                if r is not None:
                    eq += pos.qty * float(r["close"])
        return eq

    equity_rows = []

    donch_high_col = f"donch_high_{donch_entry}"
    donch_low_col = f"donch_low_{donch_exit}"
    atr_col = f"atr_{atr_len}"

    for now in timeline:
        eq = mark_to_market(now)
        equity_rows.append({"time": now, "equity": eq, "cash": cash})

        for sym in symbols:
            r = last_row(sym, now)
            if r is None:
                continue

            close = float(r["close"])

            # need indicators
            if pd.isna(r["sma_slow"]) or pd.isna(r[donch_high_col]) or pd.isna(r[donch_low_col]) or pd.isna(r[atr_col]):
                continue

            atr_val = float(r[atr_col])
            donch_high = float(r[donch_high_col])
            donch_low = float(r[donch_low_col])

            regime_on = is_regime_on(r, regime=regime)
            entry_signal = regime_on and (close > donch_high)
            exit_signal_base = (close < donch_low) or (not regime_on)

            pos = positions[sym]

            # update trailing stop if enabled and in position
            if pos.qty > 0 and trail_atr_mult and trail_atr_mult > 0 and atr_val > 0:
                new_trail = close - (trail_atr_mult * atr_val)
                pos.trail_stop = max(pos.trail_stop, new_trail) if pos.trail_stop > 0 else new_trail

            exit_signal_trail = (pos.qty > 0 and pos.trail_stop > 0 and close < pos.trail_stop)
            exit_signal = exit_signal_base or exit_signal_trail

            # EXIT
            if pos.qty > 0 and exit_signal:
                executed_price = apply_slippage(close, "sell", slippage_bps)
                notional = pos.qty * executed_price
                fee = fee_amount(notional, fee_bps)
                slip_cost = pos.qty * close * (slippage_bps / 10_000.0)  # approx

                cash += notional
                cash -= fee

                reason = "exit_donch_or_regime"
                if exit_signal_trail and not exit_signal_base:
                    reason = "exit_trailing_atr"
                elif exit_signal_trail and exit_signal_base:
                    reason = "exit_trailing_and_base"

                trades.append(
                    Trade(
                        symbol=sym,
                        side="sell",
                        time=now,
                        price=executed_price,
                        qty=pos.qty,
                        notional=notional,
                        fee=fee,
                        slippage_cost=slip_cost,
                        reason=reason,
                    )
                )

                pos.qty = 0.0
                pos.avg_price = 0.0
                pos.trail_stop = 0.0
                continue

            # ENTRY
            if pos.qty == 0 and entry_signal:
                eq_now = mark_to_market(now)
                qty = position_size_qty(
                    equity=eq_now,
                    risk_pct=risk_pct,
                    atr_val=atr_val,
                    close=close,
                    stop_atr_mult=stop_atr_mult,
                    max_order_notional=max_order_notional,
                    max_exposure_pct=max_exposure_pct,
                )
                if qty <= 0:
                    continue

                executed_price = apply_slippage(close, "buy", slippage_bps)
                notional = qty * executed_price
                fee = fee_amount(notional, fee_bps)
                total_cost = notional + fee

                # cash constraint
                if total_cost > cash and executed_price > 0:
                    qty = max((cash / (executed_price * (1.0 + fee_bps / 10_000.0))) - 1e-12, 0.0)
                    notional = qty * executed_price
                    fee = fee_amount(notional, fee_bps)
                    total_cost = notional + fee

                if qty <= 0 or total_cost > cash:
                    continue

                cash -= total_cost
                pos.qty = qty
                pos.avg_price = executed_price

                # init trailing stop (if enabled)
                if trail_atr_mult and trail_atr_mult > 0 and atr_val > 0:
                    pos.trail_stop = close - (trail_atr_mult * atr_val)
                else:
                    pos.trail_stop = 0.0

                trades.append(
                    Trade(
                        symbol=sym,
                        side="buy",
                        time=now,
                        price=executed_price,
                        qty=qty,
                        notional=notional,
                        fee=fee,
                        slippage_cost=slip_cost,
                        reason="entry_donch_breakout_regime_on",
                    )
                )
                continue

    equity_df = pd.DataFrame(equity_rows).drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)

    trades_df = pd.DataFrame([t.__dict__ for t in trades]) if trades else pd.DataFrame(
        columns=["symbol", "side", "time", "price", "qty", "notional", "fee", "slippage_cost", "reason"]
    )
    if not trades_df.empty:
        trades_df["time"] = pd.to_datetime(trades_df["time"], utc=True)

    # Round-trips FIFO por símbolo
    rts = []
    for sym in symbols:
        t = trades_df[trades_df["symbol"] == sym].sort_values("time")
        open_buy = None
        for _, row in t.iterrows():
            if row["side"] == "buy":
                open_buy = row
            elif row["side"] == "sell" and open_buy is not None:
                buy_notional = float(open_buy["notional"])
                sell_notional = float(row["notional"])
                gross = sell_notional - buy_notional
                fees = float(open_buy["fee"]) + float(row["fee"])
                net = gross - fees
                ret = net / buy_notional if buy_notional > 0 else 0.0
                hold_h = (pd.Timestamp(row["time"]) - pd.Timestamp(open_buy["time"])).total_seconds() / 3600.0
                rts.append(
                    {
                        "symbol": sym,
                        "buy_time": open_buy["time"],
                        "sell_time": row["time"],
                        "qty": float(open_buy["qty"]),
                        "buy_price": float(open_buy["price"]),
                        "sell_price": float(row["price"]),
                        "buy_notional": buy_notional,
                        "sell_notional": sell_notional,
                        "gross_pnl": gross,
                        "net_pnl": net,
                        "return_pct": ret,
                        "holding_hours": hold_h,
                    }
                )
                open_buy = None

    roundtrips_df = pd.DataFrame(rts) if rts else pd.DataFrame(
        columns=[
            "symbol","buy_time","sell_time","qty","buy_price","sell_price",
            "buy_notional","sell_notional","gross_pnl","net_pnl","return_pct","holding_hours"
        ]
    )

    return equity_df, trades_df, roundtrips_df


def print_summary(
    equity_df: pd.DataFrame,
    roundtrips_df: pd.DataFrame,
    initial_equity: float,
):
    if equity_df.empty:
        print("No equity data.")
        return

    start_t = equity_df["time"].iloc[0]
    end_t = equity_df["time"].iloc[-1]
    days = (end_t - start_t).days
    years = days / 365.25 if days > 0 else 0.0

    start_eq = float(initial_equity)
    end_eq = float(equity_df["equity"].iloc[-1])
    total_ret = (end_eq / start_eq - 1.0) if start_eq > 0 else 0.0

    mdd = max_drawdown(equity_df["equity"])
    mdd_abs = max_drawdown_abs(equity_df["equity"])
    ann = annualized_return(start_eq, end_eq, years) if years > 0 else 0.0

    print("\n===== BACKTEST SUMMARY =====")
    print(f"Period      : {start_t.date()} → {end_t.date()}  ({days} days, {years:.2f} years)")
    print(f"Start equity: {start_eq:,.2f} USDC")
    print(f"End equity  : {end_eq:,.2f} USDC")
    print(f"Total return: {total_ret*100:,.2f}%")
    print(f"Ann. return : {ann*100:,.2f}%")
    print(f"Max DD      : {mdd*100:,.2f}%  ({mdd_abs:,.2f} USDC)")

    if roundtrips_df.empty:
        print("\n--- Trades/Performance ---")
        print("Round-trips : 0")
        return

    n = len(roundtrips_df)
    wins = roundtrips_df[roundtrips_df["net_pnl"] > 0]
    losses = roundtrips_df[roundtrips_df["net_pnl"] < 0]

    winrate = len(wins) / n if n else 0.0
    net = float(roundtrips_df["net_pnl"].sum())
    avg = float(roundtrips_df["net_pnl"].mean())

    gain = float(wins["net_pnl"].sum()) if len(wins) else 0.0
    pain = float((-losses["net_pnl"]).sum()) if len(losses) else 0.0
    pf = (gain / pain) if pain > 0 else (float("inf") if gain > 0 else 0.0)

    avg_win = float(wins["net_pnl"].mean()) if len(wins) else 0.0
    avg_loss = float(losses["net_pnl"].mean()) if len(losses) else 0.0  # negativo
    expectancy = (winrate * avg_win) + ((1.0 - winrate) * avg_loss)
    avg_hold = float(roundtrips_df["holding_hours"].mean()) if "holding_hours" in roundtrips_df else 0.0

    print("\n--- Trades/Performance ---")
    print(f"Round-trips : {n}")
    print(f"Winrate     : {winrate*100:,.1f}%")
    print(f"Net PnL     : {net:,.2f} USDC")
    print(f"Avg/trade   : {avg:,.3f} USDC")
    print(f"Avg win     : {avg_win:,.3f} USDC")
    print(f"Avg loss    : {avg_loss:,.3f} USDC")
    print(f"ProfitFactor: {'∞' if pf == float('inf') else f'{pf:,.2f}'}")
    print(f"Expectancy  : {expectancy:,.3f} USDC")
    print(f"Avg hold    : {avg_hold:,.1f} h")


def main():
    p = argparse.ArgumentParser()

    p.add_argument("--symbols", default="BTC/USDC,ETH/USDC")
    p.add_argument("--timeframe", default="1d")
    p.add_argument("--start", default="2021-01-01")
    p.add_argument("--initial", type=float, default=1000.0)

    # strategy params
    p.add_argument("--regime", choices=["sma200", "sma50_gt_sma200", "sma200_slope_pos"], default="sma200")
    p.add_argument("--sma_fast", type=int, default=50)
    p.add_argument("--sma_slow", type=int, default=200)
    p.add_argument("--slope_len", type=int, default=20)

    p.add_argument("--donch_entry", type=int, default=20)
    p.add_argument("--donch_exit", type=int, default=10)
    p.add_argument("--atr_len", type=int, default=14)

    p.add_argument("--trail_atr_mult", type=float, default=0.0, help="0 desactiva trailing stop; ejemplo 3.0")

    # risk/execution
    p.add_argument("--risk_pct", type=float, default=0.01)
    p.add_argument("--stop_atr_mult", type=float, default=2.0)

    p.add_argument("--fee_bps", type=float, default=10.0)
    p.add_argument("--slippage_bps", type=float, default=5.0)

    p.add_argument("--max_order_notional", type=float, default=300.0)
    p.add_argument("--max_exposure_pct", type=float, default=0.50)

    p.add_argument("--outdir", default="backtest_out")
    args = p.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    equity_df, trades_df, roundtrips_df = run_backtest(
        symbols=symbols,
        timeframe=args.timeframe,
        start_date=args.start,
        initial_equity=args.initial,
        regime=args.regime,
        sma_fast=args.sma_fast,
        sma_slow=args.sma_slow,
        slope_len=args.slope_len,
        donch_entry=args.donch_entry,
        donch_exit=args.donch_exit,
        atr_len=args.atr_len,
        trail_atr_mult=args.trail_atr_mult,
        risk_pct=args.risk_pct,
        stop_atr_mult=args.stop_atr_mult,
        fee_bps=args.fee_bps,
        slippage_bps=args.slippage_bps,
        max_order_notional=args.max_order_notional,
        max_exposure_pct=args.max_exposure_pct,
    )

    os.makedirs(args.outdir, exist_ok=True)
    equity_path = os.path.join(args.outdir, "equity_curve.csv")
    trades_path = os.path.join(args.outdir, "trades.csv")
    rts_path = os.path.join(args.outdir, "roundtrips.csv")

    equity_df.to_csv(equity_path, index=False)
    trades_df.to_csv(trades_path, index=False)
    roundtrips_df.to_csv(rts_path, index=False)

    print_summary(equity_df, roundtrips_df, initial_equity=args.initial)

    print("\nSaved:")
    print(f" - {equity_path}")
    print(f" - {trades_path}")
    print(f" - {rts_path}")


if __name__ == "__main__":
    main()