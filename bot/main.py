"""
BOT PRINCIPAL DE TRADING
=========================

Este es el archivo principal que ejecuta el bot de trading.
Se ejecuta periódicamente (típicamente una vez al día mediante cron).

FLUJO DE EJECUCIÓN:
1. Conecta a la base de datos y crea un registro de ejecución
2. Lee configuración desde la base de datos (settings)
3. Calcula señales para todos los símbolos configurados
4. Si trading está habilitado, ejecuta órdenes según las señales
5. Envía notificaciones por Telegram
6. Registra todo en la base de datos

CONFIGURACIÓN:
- SYMBOLS: Pares a operar (ej: "BTC/USDC,ETH/USDC")
- TIMEFRAME: Timeframe de las velas (ej: "1d" para diario)
- DRY_RUN: Si es "true", no ejecuta órdenes reales (solo simula)

CONFIGURACIÓN EN BASE DE DATOS (tabla settings):
- trading_enabled: "true" para habilitar trading, "false" para solo calcular señales
- max_order_notional_usdc: Máximo valor de una orden en USDC
- max_asset_exposure_pct: Máximo % del equity que puede estar en un solo activo
- max_orders_per_day: Máximo número de órdenes por día
"""

import os
import datetime as dt
import pandas as pd

from db import get_conn, get_setting, create_run, set_run_status
from binance_client import make_exchange
from strategy import compute_indicators, decide
from risk import position_size_usdc
from notifier import telegram_send

# ============================================
# CONFIGURACIÓN DESDE VARIABLES DE ENTORNO
# ============================================
# ⚠️ MODIFICAR AQUÍ: Cambia estos valores para operar otros pares o timeframes
SYMBOLS = [s.strip() for s in os.environ.get("SYMBOLS", "BTC/USDC,ETH/USDC").split(",")]
TIMEFRAME = os.environ.get("TIMEFRAME", "1d")  # "1d" = diario, "4h" = 4 horas, etc.
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"  # "true" = simulación, "false" = órdenes reales

# Lista de seguridad: solo permite operar símbolos en esta lista
ALLOWLIST = set(SYMBOLS)


def fetch_ohlcv_df(ex, symbol: str, limit: int = 400) -> pd.DataFrame:
    """
    Obtiene datos históricos OHLCV (Open, High, Low, Close, Volume) desde Binance.
    
    Args:
        ex: Instancia del exchange (Binance)
        symbol: Par a consultar (ej: "BTC/USDC")
        limit: Número de velas a obtener (por defecto 400)
    
    Returns:
        DataFrame con columnas: ts, open, high, low, close, volume
    """
    ohlcv = ex.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def get_bot_position_qty(conn, symbol: str) -> float:
    """
    Obtiene la cantidad de un activo que tiene el bot en posición.
    
    NOTA: Esta es la posición del BOT (registrada en la BD), no necesariamente
    la posición real en Binance. El bot mantiene su propio registro de posiciones.
    
    Args:
        conn: Conexión a la base de datos
        symbol: Par a consultar (ej: "BTC/USDC")
    
    Returns:
        Cantidad del activo (0.0 si no hay posición)
    """
    with conn.cursor() as cur:
        cur.execute("SELECT qty FROM positions WHERE symbol=%s", (symbol,))
        row = cur.fetchone()
        return float(row[0]) if row else 0.0


def set_bot_position(conn, symbol: str, qty: float, avg_price: float | None = None):
    """
    Actualiza o crea el registro de posición del bot en la base de datos.
    
    Args:
        conn: Conexión a la base de datos
        symbol: Par a actualizar (ej: "BTC/USDC")
        qty: Cantidad del activo (0.0 para cerrar posición)
        avg_price: Precio promedio de entrada (None para cerrar posición)
    """
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
    """
    Cuenta cuántas órdenes se han ejecutado hoy.
    
    Se usa para respetar el límite de órdenes por día (max_orders_per_day).
    
    Args:
        conn: Conexión a la base de datos
        day: Fecha a consultar
    
    Returns:
        Número de órdenes ejecutadas ese día
    """
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM trades WHERE created_at::date=%s", (day,))
        return int(cur.fetchone()[0])


def get_bot_equity_usdc(conn, ex, symbols: list[str]) -> float:
    """
    Calcula el equity total del BOT en USDC.
    
    El equity incluye:
    - USDC disponible en la cuenta
    - + Valor de las posiciones abiertas del bot (a precio de mercado actual)
    
    NOTA: Este es el equity del BOT, no necesariamente de toda tu cuenta Binance.
    El bot solo cuenta las posiciones que él mismo ha abierto.
    
    Args:
        conn: Conexión a la base de datos
        ex: Instancia del exchange
        symbols: Lista de símbolos para calcular el valor de las posiciones
    
    Returns:
        Equity total en USDC
    """
    bal = ex.fetch_balance()
    usdc_total = float(bal["total"].get("USDC", 0.0) or 0.0)

    tickers = ex.fetch_tickers(symbols)
    equity = usdc_total

    # Sumamos el valor de las posiciones abiertas del bot
    for sym in symbols:
        last = tickers.get(sym, {}).get("last")
        if not last:
            continue
        qty = get_bot_position_qty(conn, sym)
        equity += float(qty) * float(last)

    return float(equity)


def main():
    """
    Función principal del bot.
    
    Esta función se ejecuta cada vez que el bot corre (típicamente una vez al día).
    Realiza todo el flujo: cálculo de señales, ejecución de órdenes, registro en BD.
    """
    # Conectamos a la base de datos y creamos un registro de esta ejecución
    conn = get_conn()
    run_id = create_run(conn)

    try:
        # ============================================
        # 1. LEER CONFIGURACIÓN DESDE BASE DE DATOS
        # ============================================
        # ⚠️ MODIFICAR AQUÍ: Cambia estos valores en la tabla 'settings' de la BD
        trading_enabled = get_setting(conn, "trading_enabled", "false").lower() == "true"
        max_order_notional = float(get_setting(conn, "max_order_notional_usdc", "300"))
        max_asset_exposure_pct = float(get_setting(conn, "max_asset_exposure_pct", "0.50"))
        max_orders_per_day = int(get_setting(conn, "max_orders_per_day", "2"))

        # Conectamos a Binance
        ex = make_exchange()
        ex.load_markets()

        # Validación de seguridad: solo permite operar símbolos en la allowlist
        for s in SYMBOLS:
            if s not in ALLOWLIST:
                raise RuntimeError(f"Symbol {s} fuera de allowlist.")

        today = dt.datetime.utcnow().date()

        # ============================================
        # 2. CALCULAR EQUITY Y GUARDAR SNAPSHOT
        # ============================================
        # Calculamos el equity actual del bot y lo guardamos para seguimiento histórico
        bot_equity = get_bot_equity_usdc(conn, ex, SYMBOLS)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO equity_snapshots(day, equity_usdc) VALUES(%s,%s) "
                "ON CONFLICT(day) DO UPDATE SET equity_usdc=EXCLUDED.equity_usdc",
                (today, bot_equity),
            )
        conn.commit()

        # Determinar modo de operación para el mensaje
        mode_str = (
            "LIVE" if (trading_enabled and not DRY_RUN)
            else ("PAPER" if trading_enabled else "DISABLED")
        )

        # Preparar mensajes para Telegram
        msgs = []
        msgs.append(
            f"🧠 Bot run {today} | bot_equity~{bot_equity:.2f} USDC | {mode_str} | DRY_RUN={DRY_RUN}"
        )

        # ============================================
        # 3. CALCULAR SEÑALES PARA TODOS LOS SÍMBOLOS
        # ============================================
        # ⚠️ IMPORTANTE: Esto siempre se ejecuta, incluso si trading está deshabilitado
        # Las señales se guardan en la BD para análisis histórico
        
        daily_signals = {}

        for symbol in SYMBOLS:
            # Obtener datos históricos OHLCV
            df = fetch_ohlcv_df(ex, symbol, limit=400)
            
            # Calcular indicadores técnicos (SMA200, Donchian, ATR, etc.)
            df = compute_indicators(df)
            
            # Decidir señales de entrada/salida según la estrategia
            d = decide(df, symbol)  # ← La función decide() está en strategy.py

            daily_signals[symbol] = d

            # Guardar señales en la base de datos para análisis histórico
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

            # Agregar información de señales al mensaje de Telegram
            close_str = f"{d['close']:.2f}" if d["close"] is not None else "NA"
            sma200_str = f"{d['sma200']:.2f}" if d["sma200"] is not None else "NA"
            msgs.append(
                f"• {symbol}: regime={'ON' if d['regime_on'] else 'OFF'} "
                f"entry={d['entry_signal']} exit={d['exit_signal']} close={close_str} sma200={sma200_str}"
            )

        # Si trading está deshabilitado, solo enviamos las señales y terminamos
        if not trading_enabled:
            telegram_send("\n".join(msgs))
            set_run_status(conn, run_id, "ok", "signals_only_trading_disabled")
            return

        # ============================================
        # 4. EJECUTAR TRADING (PAPER O LIVE)
        # ============================================
        # Solo llegamos aquí si trading_enabled = true
        
        # Verificar límite de órdenes por día
        if orders_today_count(conn, today) >= max_orders_per_day:
            msgs.append(f"🟡 Límite de órdenes/día alcanzado ({max_orders_per_day}). No opera hoy.")
            telegram_send("\n".join(msgs))
            set_run_status(conn, run_id, "ok", "max_orders_per_day reached")
            return

        # Obtener balance disponible en USDC
        bal = ex.fetch_balance()
        quote_free = float(bal["free"].get("USDC", 0.0) or 0.0)

        # Procesar cada símbolo
        for symbol in SYMBOLS:
            # Usamos las señales ya calculadas anteriormente
            d = daily_signals[symbol]
            close = d["close"]
            atr14 = d["atr14"]

            # Obtener posición actual del bot (desde la BD, no desde Binance)
            bot_qty = get_bot_position_qty(conn, symbol)

            # Calcular exposición actual (% del equity en este activo)
            exposure_value = (bot_qty * close) if (bot_qty > 0 and close) else 0.0
            exposure_pct = (exposure_value / bot_equity) if bot_equity > 0 else 0.0

            # ============================================
            # LÓGICA DE SALIDA (EXIT)
            # ============================================
            # Si tenemos posición y hay señal de salida, vendemos
            if bot_qty > 0 and d["exit_signal"]:
                # Determinar si ejecutamos realmente o solo simulamos
                executed = (not DRY_RUN)

                if executed:
                    # Orden real en Binance
                    order = ex.create_market_sell_order(symbol, bot_qty)
                    price = float(order.get("average") or close or 0.0)
                else:
                    # Simulación: usamos precio de cierre actual
                    price = float(close or 0.0)

                notional = bot_qty * price

                # Registrar la venta en la base de datos
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO trades(symbol,side,qty,price,notional,reason) VALUES(%s,'sell',%s,%s,%s,%s)",
                        (symbol, bot_qty, price, notional, "exit_signal_or_regime_off"),
                    )
                conn.commit()

                # Actualizar posición del bot a 0 (cerrada)
                set_bot_position(conn, symbol, 0.0, None)

                msgs.append(
                    f"🔻 SELL {symbol} qty={bot_qty:.8f} price~{price:.4f} notional~{notional:.2f} "
                    f"({'LIVE' if executed else 'PAPER'})"
                )
                continue

            # ============================================
            # LÓGICA DE ENTRADA (ENTRY)
            # ============================================
            # Si no tenemos posición y hay señal de entrada, compramos
            if bot_qty == 0 and d["entry_signal"]:
                # Validar que tenemos datos necesarios
                if not atr14 or not close:
                    msgs.append(f"⚪ {symbol}: señal entrada pero sin ATR/close válido.")
                    continue

                # ============================================
                # CALCULAR TAMAÑO DE POSICIÓN
                # ============================================
                # ⚠️ MODIFICAR AQUÍ: Cambia el riesgo (0.01 = 1%) en risk.py
                # El tamaño se calcula para arriesgar 1% del equity con stop a 2*ATR
                qty = position_size_usdc(bot_equity, 0.01, atr14, close)

                # Aplicar límites de seguridad (el más restrictivo gana)
                # 1. Límite por valor máximo de orden
                qty = min(qty, max_order_notional / close)

                # 2. Límite por exposición máxima (% del equity en un solo activo)
                qty = min(qty, (bot_equity * max_asset_exposure_pct) / close)

                # 3. Límite por USDC disponible
                qty = min(qty, (quote_free / close) if close > 0 else 0.0)

                # Si después de los límites no queda cantidad, no operamos
                if qty <= 0:
                    msgs.append(f"⚪ {symbol}: señal entrada pero qty=0 (límites o falta de USDC libre).")
                    continue

                # Determinar si ejecutamos realmente o solo simulamos
                executed = (not DRY_RUN)

                if executed:
                    # Orden real en Binance
                    order = ex.create_market_buy_order(symbol, qty)
                    price = float(order.get("average") or close)
                else:
                    # Simulación: usamos precio de cierre actual
                    price = float(close)

                notional = qty * price

                # Registrar la compra en la base de datos
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO trades(symbol,side,qty,price,notional,reason) VALUES(%s,'buy',%s,%s,%s,%s)",
                        (symbol, qty, price, notional, "donchian_breakout_regime_on"),
                    )
                conn.commit()

                # Actualizar posición del bot
                set_bot_position(conn, symbol, float(qty), float(price))

                # Consumir USDC libre en simulación para evitar doble compra en el mismo run
                quote_free -= notional

                msgs.append(
                    f"🔺 BUY {symbol} qty={qty:.8f} price~{price:.4f} notional~{notional:.2f} "
                    f"({'LIVE' if executed else 'PAPER'})"
                )
                continue

            # Si no hay señal de entrada ni salida, solo reportamos el estado
            msgs.append(
                f"• {symbol}: regime={'ON' if d['regime_on'] else 'OFF'} entry={d['entry_signal']} exit={d['exit_signal']} "
                f"bot_pos={bot_qty:.8f} exposure={exposure_pct:.0%}"
            )

        # ============================================
        # 5. ENVIAR NOTIFICACIONES Y FINALIZAR
        # ============================================
        telegram_send("\n".join(msgs))
        set_run_status(conn, run_id, "ok", "completed")

    except Exception as e:
        # En caso de error, notificar y registrar
        telegram_send(f"🔴 Bot error: {type(e).__name__}: {e}")
        set_run_status(conn, run_id, "error", f"{type(e).__name__}: {e}")
        raise
    finally:
        # Siempre cerrar la conexión a la BD
        conn.close()


if __name__ == "__main__":
    main()