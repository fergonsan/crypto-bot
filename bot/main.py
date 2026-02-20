import os
import datetime as dt
import pandas as pd

from db import get_conn, get_setting, create_run, set_run_status
from binance_client import make_exchange
from strategy import compute_indicators, decide
from risk import position_size_usdc, clamp
from notifier import telegram_send

SYMBOLS = [s.strip() for s in os.environ.get("SYMBOLS", "BTC/USDC,ETH/USDC").split(",")]
TIMEFRAME = os.environ.get("TIMEFRAME", "1d")
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

ALLOWLIST = set(SYMBOLS)

def fetch_ohlcv_df(ex, symbol: str, limit: int = 400) -> pd.DataFrame:
    ohlcv = ex.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df

def get_equity_usdc(ex) -> float:
    bal = ex.fetch_balance()
    # USDC libre + (opcional) locked, para simplificar cogemos total
    usdc = bal["total"].get("USDC", 0.0) or 0.0
    # equity total aproximado: USDC + valor de BTC/ETH en USDC
    # para simplificar, usamos tickers
    total = float(usdc)
    tickers = ex.fetch_tickers(list(ALLOWLIST))
    for sym in ["BTC/USDC", "ETH/USDC"]:
        base = sym.split("/")[0]
        qty = bal["total"].get(base, 0.0) or 0.0
        last = tickers[sym]["last"] if sym in tickers and tickers[sym]["last"] else None
        if last:
            total += float(qty) * float(last)
    return total

def orders_today_count(conn, day: dt.date) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM trades WHERE created_at::date=%s", (day,))
        return int(cur.fetchone()[0])

def main():
    conn = get_conn()
    run_id = create_run(conn)

    try:
        trading_enabled = get_setting(conn, "trading_enabled", "false").lower() == "true"
        max_order_notional = float(get_setting(conn, "max_order_notional_usdc", "300"))
        max_asset_exposure_pct = float(get_setting(conn, "max_asset_exposure_pct", "0.50"))
        max_orders_per_day = int(get_setting(conn, "max_orders_per_day", "2"))

        ex = make_exchange()
        ex.load_markets()

        today = dt.datetime.utcnow().date()
        if orders_today_count(conn, today) >= max_orders_per_day:
            telegram_send(f"🟡 Bot: límite de órdenes/día alcanzado ({max_orders_per_day}). No opera hoy.")
            set_run_status(conn, run_id, "ok", "max_orders_per_day reached")
            return

        equity = get_equity_usdc(ex)

        # snapshot equity
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO equity_snapshots(day, equity_usdc) VALUES(%s,%s) "
                "ON CONFLICT(day) DO UPDATE SET equity_usdc=EXCLUDED.equity_usdc",
                (today, equity),
            )
        conn.commit()

        bal = ex.fetch_balance()

        msgs = []
        for symbol in SYMBOLS:
            if symbol not in ALLOWLIST:
                continue

            df = fetch_ohlcv_df(ex, symbol, limit=400)
            df = compute_indicators(df)

            d = decide(df)
            close = d["close"]
            atr14 = d["atr14"]

            # guarda señal
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO signals(day,symbol,regime_on,entry_signal,exit_signal,close,sma200,donchian_high20,donchian_low10,atr14)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(day,symbol) DO UPDATE SET
                         regime_on=EXCLUDED.regime_on,
                         entry_signal=EXCLUDED.entry_signal,
                         exit_signal=EXCLUDED.exit_signal,
                         close=EXCLUDED.close,
                         sma200=EXCLUDED.sma200,
                         donchian_high20=EXCLUDED.donchian_high20,
                         donchian_low10=EXCLUDED.donchian_low10,
                         atr14=EXCLUDED.atr14
                    """,
                    (today, symbol, d["regime_on"], d["entry_signal"], d["exit_signal"],
                     d["close"], d["sma200"], d["donchian_high20"], d["donchian_low10"], d["atr14"])
                )
            conn.commit()

            base, quote = symbol.split("/")
            base_qty = float(bal["total"].get(base, 0.0) or 0.0)
            quote_qty = float(bal["free"].get(quote, 0.0) or 0.0)

            # exposición actual (aprox)
            exposure_value = 0.0
            if base_qty > 0 and close:
                exposure_value = base_qty * close
            exposure_pct = (exposure_value / equity) if equity > 0 else 0.0

            # Salida si hay posición y señal exit (incluye régimen OFF)
            if base_qty > 0 and d["exit_signal"]:
                if trading_enabled and not DRY_RUN:
                    order = ex.create_market_sell_order(symbol, base_qty)
                    price = float(order.get("average") or close or 0.0)
                else:
                    price = float(close or 0.0)

                notional = base_qty * price
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO trades(symbol,side,qty,price,notional,reason) VALUES(%s,'sell',%s,%s,%s,%s)",
                        (symbol, base_qty, price, notional, "exit_signal_or_regime_off"),
                    )
                conn.commit()
                msgs.append(f"🔻 SELL {symbol} qty={base_qty:.8f} price~{price:.4f} notional~{notional:.2f} ({'LIVE' if trading_enabled and not DRY_RUN else 'DRY'})")
                continue

            # Entrada si NO hay posición y señal entry
            if base_qty == 0 and d["entry_signal"]:
                if not atr14 or not close:
                    msgs.append(f"⚪ {symbol}: señal entrada pero sin ATR/close válido.")
                    continue

                # tamaño por riesgo 1%
                qty = position_size_usdc(equity, 0.01, atr14, close)

                # hard limit por notional
                qty_by_notional = max_order_notional / close
                qty = min(qty, qty_by_notional)

                # hard limit por exposición %
                max_exposure_value = equity * max_asset_exposure_pct
                qty_by_exposure = max_exposure_value / close
                qty = min(qty, qty_by_exposure)

                # no comprar si no hay quote suficiente
                max_affordable_qty = quote_qty / close if close > 0 else 0.0
                qty = min(qty, max_affordable_qty)

                if qty <= 0:
                    msgs.append(f"⚪ {symbol}: señal entrada pero qty=0 (límites o falta de {quote}).")
                    continue

                if trading_enabled and not DRY_RUN:
                    order = ex.create_market_buy_order(symbol, qty)
                    price = float(order.get("average") or close)
                else:
                    price = float(close)

                notional = qty * price
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO trades(symbol,side,qty,price,notional,reason) VALUES(%s,'buy',%s,%s,%s,%s)",
                        (symbol, qty, price, notional, "donchian_breakout_regime_on"),
                    )
                conn.commit()
                msgs.append(f"🔺 BUY {symbol} qty={qty:.8f} price~{price:.4f} notional~{notional:.2f} ({'LIVE' if trading_enabled and not DRY_RUN else 'DRY'})")
                continue

            # Estado
            msgs.append(f"• {symbol}: regime={'ON' if d['regime_on'] else 'OFF'} entry={d['entry_signal']} exit={d['exit_signal']} exposure={exposure_pct:.0%}")

        # Notificación resumen
        header = f"🧠 Bot run {today} | equity~{equity:.2f} USDC | {'LIVE' if (trading_enabled and not DRY_RUN) else 'DRY'}"
        telegram_send(header + "\n" + "\n".join(msgs))

        set_run_status(conn, run_id, "ok", "completed")
    except Exception as e:
        telegram_send(f"🔴 Bot error: {type(e).__name__}: {e}")
        set_run_status(conn, run_id, "error", f"{type(e).__name__}: {e}")
        raise
    finally:
        conn.close()

if __name__ == "__main__":
    main()
