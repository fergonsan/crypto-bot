"""
DAILY BOT (V3)
==============
- Señales 1d cerradas
- Entries/Exits por señal
- Actualiza hard/trailing stops en BD
- Fallback: stop por CLOSE diario (por si el intraday no corre)

Lock:
- Usa advisory lock "bot_daily" para evitar concurrente.

FIXES:
- Bug 1: Guarda donchian_high_real / donchian_low_real en signals (valores
         reales configurados, no el legacy de 20/10 períodos).
         Las columnas nuevas se añaden automáticamente si no existen (ALTER TABLE).
- Bug 2: El trail stop solo se actualiza si close y atr14 son válidos (no nulos).
         Si faltan datos ese ciclo, se avisa por Telegram pero NO se toca el stop.
"""

import os
import datetime as dt
import pandas as pd

from db import get_conn, get_setting, create_run, set_run_status, try_advisory_lock, release_advisory_lock
from binance_client import make_exchange
from strategy import compute_indicators, decide
from risk import position_size_usdc
from notifier import telegram_send


SYMBOLS = [s.strip() for s in os.environ.get("SYMBOLS", "BTC/USDC,ETH/USDC").split(",") if s.strip()]
TIMEFRAME = os.environ.get("TIMEFRAME", "1d")
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

os.environ.setdefault("DONCH_ENTRY", "55")
os.environ.setdefault("DONCH_EXIT", "20")

RISK_PER_TRADE = float(os.environ.get("RISK_PER_TRADE", "0.02"))
HARD_STOP_ATR_MULT = float(os.environ.get("HARD_STOP_ATR_MULT", "1.5"))
TRAIL_ATR_MULT = float(os.environ.get("TRAIL_ATR_MULT", "3.0"))

ALLOWLIST = set(SYMBOLS)


def _symbol_prefix(symbol: str) -> str:
    """Extrae el prefijo de variable de entorno del símbolo: 'BTC/USDC' → 'BTC'."""
    return symbol.split("/")[0].upper()


def _sym_float(prefix: str, name: str, global_val: float) -> float:
    """Lee {PREFIX}_{NAME} de env; si no existe, devuelve global_val."""
    v = os.environ.get(f"{prefix}_{name}")
    if v is not None:
        try:
            return float(v)
        except Exception:
            pass
    return global_val


def _sym_int(prefix: str, name: str, global_val: int) -> int:
    """Lee {PREFIX}_{NAME} de env; si no existe, devuelve global_val."""
    v = os.environ.get(f"{prefix}_{name}")
    if v is not None:
        try:
            return int(v)
        except Exception:
            pass
    return global_val


def _symbol_config(symbol: str) -> dict:
    """Devuelve el dict de parámetros para el símbolo dado, con fallback a globales."""
    prefix = _symbol_prefix(symbol)
    return {
        "donch_entry": _sym_int(prefix, "DONCH_ENTRY", int(os.environ.get("DONCH_ENTRY", "55"))),
        "donch_exit": _sym_int(prefix, "DONCH_EXIT", int(os.environ.get("DONCH_EXIT", "20"))),
        "risk_per_trade": _sym_float(prefix, "RISK_PER_TRADE", RISK_PER_TRADE),
        "hard_stop_atr_mult": _sym_float(prefix, "HARD_STOP_ATR_MULT", HARD_STOP_ATR_MULT),
        "trail_atr_mult": _sym_float(prefix, "TRAIL_ATR_MULT", TRAIL_ATR_MULT),
    }


def fetch_ohlcv_df(ex, symbol: str, limit: int = 500) -> pd.DataFrame:
    ohlcv = ex.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def get_bot_position(conn, symbol: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, qty, avg_price, entry_time, peak_close, hard_stop, trail_stop "
            "FROM positions WHERE symbol=%s",
            (symbol,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "symbol": row[0],
            "qty": float(row[1] or 0.0),
            "entry_price": float(row[2] or 0.0),
            "entry_time": row[3],
            "peak_close": float(row[4] or 0.0),
            "hard_stop": float(row[5] or 0.0),
            "trail_stop": float(row[6] or 0.0),
        }


def upsert_bot_position(conn, symbol: str, qty: float, entry_price: float | None,
                        entry_time=None, peak_close: float = 0.0,
                        hard_stop: float = 0.0, trail_stop: float = 0.0):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO positions(symbol, qty, avg_price, entry_time, peak_close, hard_stop, trail_stop, updated_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT(symbol) DO UPDATE SET
              qty=EXCLUDED.qty,
              avg_price=EXCLUDED.avg_price,
              entry_time=EXCLUDED.entry_time,
              peak_close=EXCLUDED.peak_close,
              hard_stop=EXCLUDED.hard_stop,
              trail_stop=EXCLUDED.trail_stop,
              updated_at=NOW()
            """,
            (symbol, qty, entry_price, entry_time, peak_close, hard_stop, trail_stop),
        )
    conn.commit()


def orders_today_count(conn, day: dt.date) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM trades WHERE created_at::date=%s", (day,))
        return int(cur.fetchone()[0])


def get_bot_equity_usdc(conn, ex, symbols: list[str]) -> float:
    bal = ex.fetch_balance()
    usdc_total = float(bal["total"].get("USDC", 0.0) or 0.0)
    tickers = ex.fetch_tickers(symbols)
    equity = usdc_total
    for sym in symbols:
        last = tickers.get(sym, {}).get("last")
        if not last:
            continue
        pos = get_bot_position(conn, sym)
        qty = float(pos["qty"]) if pos else 0.0
        equity += qty * float(last)
    return float(equity)


def ensure_signals_columns(conn):
    """
    Bug 1 fix: añade columnas nuevas a signals si no existen.
    Permite deploy incremental sin modificar schema.sql manualmente.
    """
    new_cols = [
        ("donchian_high_real", "NUMERIC"),
        ("donchian_low_real",  "NUMERIC"),
        ("donch_entry_n",      "INTEGER"),
        ("donch_exit_n",       "INTEGER"),
    ]
    with conn.cursor() as cur:
        for col, dtype in new_cols:
            cur.execute(f"ALTER TABLE signals ADD COLUMN IF NOT EXISTS {col} {dtype}")
    conn.commit()


def main():
    conn = get_conn()
    run_id = create_run(conn)
    lock_key = "bot_daily"

    try:
        if not try_advisory_lock(conn, lock_key):
            set_run_status(conn, run_id, "ok", "lock_not_acquired")
            return

        # Bug 1 fix: asegurar columnas nuevas antes de cualquier INSERT
        ensure_signals_columns(conn)

        trading_enabled = get_setting(conn, "trading_enabled", "false").lower() == "true"
        max_order_notional = float(get_setting(conn, "max_order_notional_usdc", "300"))
        max_asset_exposure_pct = float(get_setting(conn, "max_asset_exposure_pct", "0.50"))
        max_orders_per_day = int(get_setting(conn, "max_orders_per_day", "2"))

        ex = make_exchange()
        ex.load_markets()

        for s in SYMBOLS:
            if s not in ALLOWLIST:
                raise RuntimeError(f"Symbol {s} fuera de allowlist.")

        today = dt.datetime.utcnow().date()

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
        msgs = [
            f"🧠 DAILY {today} | equity~{bot_equity:.2f} USDC | {mode_str} | DRY_RUN={DRY_RUN}",
            f"⚙️ V3: Donch={os.environ.get('DONCH_ENTRY')}/{os.environ.get('DONCH_EXIT')} "
            f"risk={RISK_PER_TRADE:.4f} hardATR={HARD_STOP_ATR_MULT:.2f} trailATR={TRAIL_ATR_MULT:.2f}",
        ]

        # Señales
        daily_signals = {}
        symbol_configs = {}
        for symbol in SYMBOLS:
            cfg = _symbol_config(symbol)
            symbol_configs[symbol] = cfg
            df = fetch_ohlcv_df(ex, symbol, limit=500)
            df = compute_indicators(df, donch_entry=cfg["donch_entry"], donch_exit=cfg["donch_exit"])
            d = decide(df, symbol, donch_entry=cfg["donch_entry"], donch_exit=cfg["donch_exit"])
            daily_signals[symbol] = d

            # Bug 1 fix: guardar también los valores reales del Donchian configurado
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO signals(
                        day, symbol, regime_on, entry_signal, exit_signal,
                        close, sma200,
                        donchian_high20, donchian_low10,
                        donchian_high_real, donchian_low_real,
                        donch_entry_n, donch_exit_n,
                        atr14
                    )
                    VALUES(%s,%s,%s,%s,%s, %s,%s, %s,%s, %s,%s, %s,%s, %s)
                    ON CONFLICT(day,symbol) DO UPDATE SET
                        regime_on=EXCLUDED.regime_on,
                        entry_signal=EXCLUDED.entry_signal,
                        exit_signal=EXCLUDED.exit_signal,
                        close=EXCLUDED.close,
                        sma200=EXCLUDED.sma200,
                        donchian_high20=EXCLUDED.donchian_high20,
                        donchian_low10=EXCLUDED.donchian_low10,
                        donchian_high_real=EXCLUDED.donchian_high_real,
                        donchian_low_real=EXCLUDED.donchian_low_real,
                        donch_entry_n=EXCLUDED.donch_entry_n,
                        donch_exit_n=EXCLUDED.donch_exit_n,
                        atr14=EXCLUDED.atr14
                    """,
                    (
                        today, symbol, d["regime_on"], d["entry_signal"], d["exit_signal"],
                        d["close"], d["sma200"],
                        d["donchian_high20"], d["donchian_low10"],
                        d["donchian_high"], d["donchian_low"],
                        d["donch_entry"], d["donch_exit"],
                        d["atr14"],
                    ),
                )
            conn.commit()

        if not trading_enabled:
            telegram_send("\n".join(msgs))
            set_run_status(conn, run_id, "ok", "signals_only_trading_disabled")
            return

        if orders_today_count(conn, today) >= max_orders_per_day:
            msgs.append(f"🟡 max_orders_per_day alcanzado ({max_orders_per_day}).")
            telegram_send("\n".join(msgs))
            set_run_status(conn, run_id, "ok", "max_orders_per_day reached")
            return

        bal = ex.fetch_balance()
        quote_free = float(bal["free"].get("USDC", 0.0) or 0.0)

        for symbol in SYMBOLS:
            d = daily_signals[symbol]
            cfg = symbol_configs[symbol]
            close = d["close"]
            atr14 = d["atr14"]

            pos = get_bot_position(conn, symbol)
            bot_qty = float(pos["qty"]) if pos else 0.0

            # Actualizar stops + fallback stop por close diario
            if bot_qty > 0:
                # Bug 2 fix: solo actualizar trail si tenemos datos válidos
                if close is not None and atr14 is not None and float(close) > 0 and float(atr14) > 0:
                    peak_close = max(float(pos.get("peak_close") or 0.0), float(close))
                    trail_candidate = peak_close - (cfg["trail_atr_mult"] * float(atr14))
                    trail_stop = max(float(pos.get("trail_stop") or 0.0), float(trail_candidate))

                    hard_stop = float(pos.get("hard_stop") or 0.0)
                    if hard_stop <= 0.0:
                        hard_stop = float(pos["entry_price"]) - (cfg["hard_stop_atr_mult"] * float(atr14))

                    stop_level = max(hard_stop, trail_stop)

                    upsert_bot_position(
                        conn, symbol, bot_qty, float(pos["entry_price"]),
                        entry_time=pos.get("entry_time"), peak_close=peak_close,
                        hard_stop=hard_stop, trail_stop=trail_stop,
                    )

                    if float(close) < stop_level:
                        executed = (not DRY_RUN)
                        if executed:
                            order = ex.create_market_sell_order(symbol, bot_qty)
                            price = float(order.get("average") or close or 0.0)
                        else:
                            price = float(close)

                        notional = bot_qty * price
                        with conn.cursor() as cur:
                            cur.execute(
                                "INSERT INTO trades(symbol,side,qty,price,notional,reason) "
                                "VALUES(%s,'sell',%s,%s,%s,%s)",
                                (symbol, bot_qty, price, notional, "stop_close_daily"),
                            )
                        conn.commit()

                        upsert_bot_position(conn, symbol, 0.0, None, entry_time=None,
                                            peak_close=0.0, hard_stop=0.0, trail_stop=0.0)
                        msgs.append(
                            f"🛑 STOP(daily close) {symbol} qty={bot_qty:.8f} "
                            f"px~{price:.4f} stop~{stop_level:.4f} "
                            f"({'LIVE' if executed else 'PAPER'})"
                        )
                        continue

                else:
                    # Bug 2 fix: datos nulos → no tocar stops, avisar
                    msgs.append(
                        f"⚠️ {symbol}: posición abierta pero close/atr14 nulos — "
                        f"stops NO actualizados este ciclo."
                    )

            # Exit por señal
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
                        "INSERT INTO trades(symbol,side,qty,price,notional,reason) "
                        "VALUES(%s,'sell',%s,%s,%s,%s)",
                        (symbol, bot_qty, price, notional, "exit_signal_or_regime_off"),
                    )
                conn.commit()

                upsert_bot_position(conn, symbol, 0.0, None, entry_time=None,
                                    peak_close=0.0, hard_stop=0.0, trail_stop=0.0)
                msgs.append(
                    f"🔻 SELL {symbol} qty={bot_qty:.8f} px~{price:.4f} "
                    f"({'LIVE' if executed else 'PAPER'})"
                )
                continue

            # Entry
            if bot_qty == 0 and d["entry_signal"]:
                if not atr14 or not close:
                    msgs.append(f"⚪ {symbol}: entry pero sin ATR/close.")
                    continue

                qty = position_size_usdc(
                    bot_equity, cfg["risk_per_trade"], float(atr14), float(close),
                    hard_stop_atr_mult=cfg["hard_stop_atr_mult"],
                )
                qty = min(qty, max_order_notional / float(close))
                qty = min(qty, (bot_equity * max_asset_exposure_pct) / float(close))
                qty = min(qty, (quote_free / float(close)) if float(close) > 0 else 0.0)

                if qty <= 0:
                    msgs.append(f"⚪ {symbol}: entry pero qty=0 (límites/cash).")
                    continue

                executed = (not DRY_RUN)
                if executed:
                    order = ex.create_market_buy_order(symbol, qty)
                    price = float(order.get("average") or close)
                else:
                    price = float(close)

                notional = qty * price
                entry_time = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
                hard_stop = float(price) - (cfg["hard_stop_atr_mult"] * float(atr14))
                peak_close = float(price)
                trail_stop = peak_close - (cfg["trail_atr_mult"] * float(atr14))

                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO trades(symbol,side,qty,price,notional,reason) "
                        "VALUES(%s,'buy',%s,%s,%s,%s)",
                        (symbol, qty, price, notional, "donchian_breakout_regime_on"),
                    )
                conn.commit()

                upsert_bot_position(
                    conn, symbol, float(qty), float(price), entry_time=entry_time,
                    peak_close=peak_close, hard_stop=hard_stop, trail_stop=trail_stop,
                )
                quote_free -= notional
                msgs.append(
                    f"🔺 BUY {symbol} qty={qty:.8f} px~{price:.4f} "
                    f"hard~{hard_stop:.4f} trail~{trail_stop:.4f} "
                    f"({'LIVE' if executed else 'PAPER'})"
                )
                continue

        telegram_send("\n".join(msgs))
        set_run_status(conn, run_id, "ok", "completed")

    except Exception as e:
        telegram_send(f"🔴 DAILY error: {type(e).__name__}: {e}")
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