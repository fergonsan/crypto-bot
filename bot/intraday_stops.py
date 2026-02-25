"""
INTRADAY STOPS (V3) - stop_on_low
=================================
Ejecuta cada X minutos y solo dispara stops:
- stop_level = max(hard_stop, trail_stop)
- obtiene última vela intradía (default 5m)
- si low <= stop_level -> market sell

Lock:
- Usa advisory lock "bot_intraday_stops".
"""

import os
import datetime as dt
import pandas as pd

from db import get_conn, create_run, set_run_status, try_advisory_lock, release_advisory_lock
from binance_client import make_exchange
from notifier import telegram_send


SYMBOLS = [s.strip() for s in os.environ.get("SYMBOLS", "BTC/USDC,ETH/USDC").split(",") if s.strip()]
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

STOP_CHECK_TIMEFRAME = os.environ.get("STOP_CHECK_TIMEFRAME", "5m")
STOP_CHECK_LIMIT = int(os.environ.get("STOP_CHECK_LIMIT", "2"))

ALLOWLIST = set(SYMBOLS)


def fetch_last_intraday_candle(ex, symbol: str):
    ohlcv = ex.fetch_ohlcv(symbol, timeframe=STOP_CHECK_TIMEFRAME, limit=STOP_CHECK_LIMIT)
    if not ohlcv:
        return None
    row = ohlcv[-1]
    ts = pd.to_datetime(row[0], unit="ms", utc=True)
    return {
        "ts": ts,
        "open": float(row[1]),
        "high": float(row[2]),
        "low": float(row[3]),
        "close": float(row[4]),
    }


def get_open_positions(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, qty, avg_price, entry_time, peak_close, hard_stop, trail_stop "
            "FROM positions WHERE qty > 0"
        )
        rows = cur.fetchall() or []
        out = []
        for r in rows:
            out.append(
                {
                    "symbol": r[0],
                    "qty": float(r[1] or 0.0),
                    "entry_price": float(r[2] or 0.0),
                    "entry_time": r[3],
                    "peak_close": float(r[4] or 0.0),
                    "hard_stop": float(r[5] or 0.0),
                    "trail_stop": float(r[6] or 0.0),
                }
            )
        return out


def close_position(conn, symbol: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE positions
               SET qty=0, avg_price=NULL, entry_time=NULL,
                   peak_close=0, hard_stop=0, trail_stop=0,
                   updated_at=NOW()
             WHERE symbol=%s
            """,
            (symbol,),
        )
    conn.commit()


def main():
    conn = get_conn()
    run_id = create_run(conn)
    lock_key = "bot_intraday_stops"

    try:
        if not try_advisory_lock(conn, lock_key):
            set_run_status(conn, run_id, "ok", "lock_not_acquired")
            return

        ex = make_exchange()
        ex.load_markets()

        for s in SYMBOLS:
            if s not in ALLOWLIST:
                raise RuntimeError(f"Symbol {s} fuera de allowlist.")

        positions = get_open_positions(conn)
        if not positions:
            set_run_status(conn, run_id, "ok", "no_positions")
            return

        msgs = []
        msgs.append(f"🛡️ INTRADAY stops | tf={STOP_CHECK_TIMEFRAME} | DRY_RUN={DRY_RUN} | positions={len(positions)}")

        for p in positions:
            sym = p["symbol"]
            qty = float(p["qty"])
            stop_level = max(float(p["hard_stop"] or 0.0), float(p["trail_stop"] or 0.0))
            if qty <= 0 or stop_level <= 0:
                continue

            candle = fetch_last_intraday_candle(ex, sym)
            if candle is None:
                ticker = ex.fetch_ticker(sym)
                last = float(ticker.get("last") or 0.0)
                low = last
                cts = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
            else:
                low = float(candle["low"])
                last = float(candle["close"])
                cts = candle["ts"]

            if low <= stop_level:
                executed = (not DRY_RUN)
                if executed:
                    order = ex.create_market_sell_order(sym, qty)
                    price = float(order.get("average") or last or stop_level)
                else:
                    price = float(stop_level)

                notional = qty * price

                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO trades(symbol,side,qty,price,notional,reason) VALUES(%s,'sell',%s,%s,%s,%s)",
                        (sym, qty, price, notional, "stop_intraday_low"),
                    )
                conn.commit()

                close_position(conn, sym)

                msgs.append(
                    f"🛑 STOP {sym} qty={qty:.8f} px~{price:.4f} notional~{notional:.2f} "
                    f"low~{low:.4f} stop~{stop_level:.4f} ts={cts} ({'LIVE' if executed else 'PAPER'})"
                )
            else:
                msgs.append(f"• {sym}: ok low~{low:.4f} stop~{stop_level:.4f} last~{last:.4f}")

        telegram_send("\n".join(msgs))
        set_run_status(conn, run_id, "ok", "completed_intraday")

    except Exception as e:
        telegram_send(f"🔴 INTRADAY error: {type(e).__name__}: {e}")
        set_run_status(conn, run_id, "error", f"{type(e).__name__}: {e}")
        raise
    finally:
        try:
            release_advisory_lock(conn, lock_key)
        except Exception:
            pass
        conn.close()


if __name__ == "__main__":
    main()