import os
import datetime as dt
import pandas as pd

from db import get_conn, get_setting, create_run, set_run_status
from binance_client import make_exchange
from strategy import compute_indicators, decide
from risk import position_size_usdc
from notifier import telegram_send

SYMBOLS = [s.strip() for s in os.environ.get("SYMBOLS", "BTC/USDC,ETH/USDC").split(",")]
TIMEFRAME = os.environ.get("TIMEFRAME", "1d")
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

ALLOWLIST = set(SYMBOLS)


def fetch_ohlcv_df(ex, symbol: str, limit: int = 400) -> pd.DataFrame:
    ohlcv = ex.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def get_bot_position_qty(conn, symbol: str) -> float:
    with conn.cursor() as cur:
        cur.execute("SELECT qty FROM positions WHERE symbol=%s", (symbol,))
        row = cur.fetchone()
        return float(row[0]) if row else 0.0


def set_bot_position(conn, symbol: str, qty: float, avg_price: float | None = None):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO positions(symbol, qty, avg_price, updated_at)
            VALUES(%s, %s, %s, NOW())
            ON CONFLICT(symbol) DO UPDATE SET
              qty=EXCLUDED.qty,
              avg_price=EXCLUDED.avg_price,
              updated_at=NOW()
            """,
            (symbol, qty, avg_price),
        )
    conn.commit()


def orders_today_count(conn, day: dt.date) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM trades WHERE created_at::date=%s", (day,))
        return int(cur.fetchone()[0])


def get_bot_equity_usdc(conn, ex, symbols: list[str]) -> float:
    """
    Equity del BOT (no de tu cuenta completa):
      - USDC total
      - + valor de las posiciones del BOT (tabla positions) a precio last
    """
    bal = ex.fetch_balance()
    usdc_total = float(bal["total"].get("USDC", 0.0) or 0.0)

    tickers = ex.fetch_tickers(symbols)
    equity = usdc_total

    for sym in symbols:
        last = tickers.get(sym, {}).get("last")
        if not last:
            continue
        qty = get_bot_position_qty(conn, sym)
        equity += float(qty) * float(last)

    return float(equity)


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

        # Validación allowlist
        for s in SYMBOLS:
            if s not in ALLOWLIST:
                raise RuntimeError(f"Symbol {s} fuera de allowlist.")

        today = dt.datetime.utcnow().date()

        # Equity snapshot (del BOT)
        bot_equity = get_bot_equity_usdc(conn, ex, SYMBOLS)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO equity_snapshots(day, equity_usdc) VALUES(%s,%s) "
                "ON CONFLICT(day) DO UPDATE SET equity_usdc=EXCLUDED.equity_usdc",
                (today, bot_equity),
            )
        conn.commit()

        mode_str = (
            "LIVE" if (trading_enabled and not DRY_RUN)
            else ("PAPER" if trading_enabled else "DISABLED")
        )

        msgs = []
        msgs.append(
            f"🧠 Bot run {today} | bot_equity~{bot_equity:.2f} USDC | {mode_str} | DRY_RUN={DRY_RUN}"
        )

        # ============================
        # 1) SIEMPRE: calcular y guardar señales
        # ============================
        daily_signals = {}

        for symbol in SYMBOLS:
            df = fetch_ohlcv_df(ex, symbol, limit=400)
            df = compute_indicators(df)
            d = decide(df, symbol)  # <- IMPORTANTE: symbol aquí

            daily_signals[symbol] = d

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
                    (
                        today,
                        symbol,
                        d["regime_on"],
                        d["entry_signal"],
                        d["exit_signal"],
                        d["close"],
                        d["sma200"],
                        d["donchian_high20"],
                        d["donchian_low10"],
                        d["atr14"],
                    ),
                )
            conn.commit()

            close_str = f"{d['close']:.2f}" if d["close"] is not None else "NA"
            sma200_str = f"{d['sma200']:.2f}" if d["sma200"] is not None else "NA"
            msgs.append(
                f"• {symbol}: regime={'ON' if d['regime_on'] else 'OFF'} "
                f"entry={d['entry_signal']} exit={d['exit_signal']} close={close_str} sma200={sma200_str}"
            )

        # Si está deshabilitado: solo señales
        if not trading_enabled:
            telegram_send("\n".join(msgs))
            set_run_status(conn, run_id, "ok", "signals_only_trading_disabled")
            return

        # ============================
        # 2) TRADING (paper o live)
        # ============================

        if orders_today_count(conn, today) >= max_orders_per_day:
            msgs.append(f"🟡 Límite de órdenes/día alcanzado ({max_orders_per_day}). No opera hoy.")
            telegram_send("\n".join(msgs))
            set_run_status(conn, run_id, "ok", "max_orders_per_day reached")
            return

        bal = ex.fetch_balance()
        quote_free = float(bal["free"].get("USDC", 0.0) or 0.0)

        for symbol in SYMBOLS:
            d = daily_signals[symbol]  # usamos lo ya calculado
            close = d["close"]
            atr14 = d["atr14"]

            # Posición del BOT (NO del balance real)
            bot_qty = get_bot_position_qty(conn, symbol)

            exposure_value = (bot_qty * close) if (bot_qty > 0 and close) else 0.0
            exposure_pct = (exposure_value / bot_equity) if bot_equity > 0 else 0.0

            # EXIT
            if bot_qty > 0 and d["exit_signal"]:
                executed = (not DRY_RUN)

                if executed:
                    order = ex.create_market_sell_order(symbol, bot_qty)
                    price = float(order.get("average") or close or 0.0)
                else:
                    price = float(close or 0.0)

                notional = bot_qty * price

                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO trades(symbol,side,qty,price,notional,reason) VALUES(%s,'sell',%s,%s,%s,%s)",
                        (symbol, bot_qty, price, notional, "exit_signal_or_regime_off"),
                    )
                conn.commit()

                set_bot_position(conn, symbol, 0.0, None)

                msgs.append(
                    f"🔻 SELL {symbol} qty={bot_qty:.8f} price~{price:.4f} notional~{notional:.2f} "
                    f"({'LIVE' if executed else 'PAPER'})"
                )
                continue

            # ENTRY
            if bot_qty == 0 and d["entry_signal"]:
                if not atr14 or not close:
                    msgs.append(f"⚪ {symbol}: señal entrada pero sin ATR/close válido.")
                    continue

                # 1% de riesgo con stop 2*ATR
                qty = position_size_usdc(bot_equity, 0.01, atr14, close)

                # hard limit por notional
                qty = min(qty, max_order_notional / close)

                # hard limit por exposición %
                qty = min(qty, (bot_equity * max_asset_exposure_pct) / close)

                # hard limit por USDC libre
                qty = min(qty, (quote_free / close) if close > 0 else 0.0)

                if qty <= 0:
                    msgs.append(f"⚪ {symbol}: señal entrada pero qty=0 (límites o falta de USDC libre).")
                    continue

                executed = (not DRY_RUN)

                if executed:
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

                set_bot_position(conn, symbol, float(qty), float(price))

                # consumir USDC libre en papel para evitar doble compra en el mismo run
                quote_free -= notional

                msgs.append(
                    f"🔺 BUY {symbol} qty={qty:.8f} price~{price:.4f} notional~{notional:.2f} "
                    f"({'LIVE' if executed else 'PAPER'})"
                )
                continue

            msgs.append(
                f"• {symbol}: regime={'ON' if d['regime_on'] else 'OFF'} entry={d['entry_signal']} exit={d['exit_signal']} "
                f"bot_pos={bot_qty:.8f} exposure={exposure_pct:.0%}"
            )

        telegram_send("\n".join(msgs))
        set_run_status(conn, run_id, "ok", "completed")

    except Exception as e:
        telegram_send(f"🔴 Bot error: {type(e).__name__}: {e}")
        set_run_status(conn, run_id, "error", f"{type(e).__name__}: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()