"""
MÓDULO DE ESTRATEGIA DE TRADING
================================

Este módulo contiene toda la lógica de la estrategia de trading:
- Cálculo de indicadores técnicos
- Lógica de señales de entrada y salida
- Filtro de régimen (cuándo operar y cuándo no)

⚠️ IMPORTANTE: Este es el archivo principal que debes modificar para cambiar la estrategia.
"""

import os
import pandas as pd


def sma(series: pd.Series, n: int) -> pd.Series:
    """
    Calcula la Media Móvil Simple (Simple Moving Average).
    
    Args:
        series: Serie de precios (típicamente precios de cierre)
        n: Período de la media móvil
    
    Returns:
        Serie con los valores de la SMA
    """
    return series.rolling(n).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    """
    Calcula el True Range (Rango Verdadero).
    
    El True Range es el máximo de:
    1. Alto - Bajo (del período actual)
    2. |Alto - Cierre anterior|
    3. |Bajo - Cierre anterior|
    
    Se usa para medir la volatilidad del mercado.
    
    Args:
        df: DataFrame con columnas 'high', 'low', 'close'
    
    Returns:
        Serie con los valores de True Range
    """
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
    """
    Calcula el Average True Range (ATR) - Promedio del Rango Verdadero.
    
    El ATR es una media móvil del True Range y mide la volatilidad promedio.
    Se usa para calcular stops y tamaño de posición.
    
    Args:
        df: DataFrame con columnas 'high', 'low', 'close'
        n: Período para la media móvil (típicamente 14)
    
    Returns:
        Serie con los valores de ATR
    """
    return true_range(df).rolling(n).mean()


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calcula todos los indicadores técnicos necesarios para la estrategia.
    
    ⚠️ MODIFICAR AQUÍ: Si quieres agregar nuevos indicadores, hazlo en esta función.
    
    Indicadores actuales:
    - sma200: Media móvil simple de 200 períodos (filtro de tendencia)
    - donchian_high20: Canal de Donchian alto de 20 períodos (para señales de entrada)
    - donchian_low10: Canal de Donchian bajo de 10 períodos (para señales de salida)
    - atr14: Average True Range de 14 períodos (para gestión de riesgo)
    
    Args:
        df: DataFrame con columnas OHLCV (open, high, low, close, volume)
    
    Returns:
        DataFrame con las columnas originales más las nuevas columnas de indicadores
    """
    df = df.copy()
    # Media móvil de 200 períodos - se usa como filtro de régimen
    df["sma200"] = sma(df["close"], 200)
    
    # Canal de Donchian: máximo de los últimos 20 períodos (desplazado 1 día)
    # Se usa para detectar breakouts alcistas
    df["donchian_high20"] = df["high"].shift(1).rolling(20).max()
    
    # Canal de Donchian: mínimo de los últimos 10 períodos (desplazado 1 día)
    # Se usa para detectar señales de salida
    df["donchian_low10"] = df["low"].shift(1).rolling(10).min()
    
    # ATR de 14 períodos - se usa para calcular stops y tamaño de posición
    df["atr14"] = atr(df, 14)
    
    return df


def _env_flag(name: str, default: str = "false") -> bool:
    """
    Función auxiliar para leer flags booleanos desde variables de entorno.
    
    Args:
        name: Nombre de la variable de entorno
        default: Valor por defecto si no existe la variable
    
    Returns:
        True si la variable está en "true" (case insensitive), False en caso contrario
    """
    return os.environ.get(name, default).lower() == "true"


def decide(df: pd.DataFrame, symbol: str | None = None):
    """
    ⚠️ FUNCIÓN PRINCIPAL DE DECISIÓN - MODIFICA AQUÍ LA LÓGICA DE ENTRADA/SALIDA ⚠️
    
    Esta función toma los datos históricos con indicadores y decide:
    1. Si el "régimen" está activo (si debemos operar o no)
    2. Si hay señal de ENTRADA (comprar)
    3. Si hay señal de SALIDA (vender)
    
    ESTRATEGIA ACTUAL:
    ------------------
    - Régimen ON: El precio de cierre está POR ENCIMA de la SMA200 (mercado alcista)
    - Señal ENTRADA: Régimen ON + precio rompe por encima del máximo de 20 períodos (Donchian High)
    - Señal SALIDA: Precio cae por debajo del mínimo de 10 períodos (Donchian Low) O régimen se apaga
    
    Para modificar la estrategia:
    1. Cambia las condiciones de regime_on (línea ~63)
    2. Cambia las condiciones de entry_signal (línea ~66)
    3. Cambia las condiciones de exit_signal (línea ~67)
    
    Args:
        df: DataFrame con indicadores calculados (debe tener columnas de compute_indicators)
        symbol: Símbolo del par (ej: "BTC/USDC") - usado solo para modo test
    
    Returns:
        Dict con:
        - regime_on (bool): Si el régimen está activo
        - entry_signal (bool): Si hay señal de compra
        - exit_signal (bool): Si hay señal de venta
        - close, sma200, donchian_high20, donchian_low10, atr14: Valores actuales
    
    MODO TEST (variables de entorno para pruebas):
    ----------------------------------------------
      TEST_MODE=true habilita el modo de prueba.
      TEST_FORCE_REGIME_ON=true fuerza regime_on=True.
      TEST_FORCE_ENTRY_SYMBOL="BTC/USDC" fuerza entry_signal=True para ese símbolo.
      TEST_FORCE_EXIT_SYMBOL="BTC/USDC" fuerza exit_signal=True para ese símbolo.
      TEST_IGNORE_EXIT=true ignora el exit natural cuando estás forzando entrada.
    """
    # Tomamos la última fila (datos más recientes)
    row = df.iloc[-1]

    # Extraemos los valores actuales de los indicadores
    close = float(row["close"]) if pd.notna(row["close"]) else None
    sma200_v = float(row["sma200"]) if pd.notna(row["sma200"]) else None
    high20 = float(row["donchian_high20"]) if pd.notna(row["donchian_high20"]) else None
    low10 = float(row["donchian_low10"]) if pd.notna(row["donchian_low10"]) else None
    atr14_v = float(row["atr14"]) if pd.notna(row["atr14"]) else None

    # ============================================
    # LÓGICA DE RÉGIMEN (FILTRO DE TENDENCIA)
    # ============================================
    # ⚠️ MODIFICAR AQUÍ: Cambia esta condición para cambiar cuándo el bot opera
    # Actualmente: Solo opera cuando el precio está por encima de SMA200 (tendencia alcista)
    if sma200_v is None or close is None:
        regime_on = False
    else:
        regime_on = close > sma200_v  # ← Cambia esta línea para otro filtro de régimen

    # ============================================
    # LÓGICA DE SEÑALES DE ENTRADA Y SALIDA
    # ============================================
    # ⚠️ MODIFICAR AQUÍ: Cambia estas condiciones para cambiar cuándo comprar/vender
    
    # ENTRADA: Régimen activo + precio rompe el máximo de 20 períodos (breakout alcista)
    entry_signal = bool(regime_on and (high20 is not None) and (close is not None) and (close > high20))
    
    # SALIDA: Precio cae por debajo del mínimo de 10 períodos O régimen se apaga
    exit_signal = bool(((low10 is not None) and (close is not None) and (close < low10)) or (not regime_on))

    # ============================================
    # MODO TEST (solo para pruebas/debugging)
    # ============================================
    # Este bloque permite forzar señales mediante variables de entorno
    # Útil para probar el bot sin esperar condiciones reales del mercado
    test_mode = _env_flag("TEST_MODE", "false")
    if test_mode:
        force_regime = _env_flag("TEST_FORCE_REGIME_ON", "false")
        ignore_exit = _env_flag("TEST_IGNORE_EXIT", "false")
        force_entry_sym = os.environ.get("TEST_FORCE_ENTRY_SYMBOL", "").strip()
        force_exit_sym = os.environ.get("TEST_FORCE_EXIT_SYMBOL", "").strip()

        if force_regime:
            regime_on = True

        # Si régimen OFF, entry debe ser False
        if not regime_on:
            entry_signal = False

        # Forzar EXIT explícito
        if symbol and force_exit_sym and symbol == force_exit_sym:
            exit_signal = True

        # Forzar ENTRY explícito (y opcionalmente ignorar exit natural)
        if symbol and force_entry_sym and symbol == force_entry_sym:
            if ignore_exit:
                exit_signal = False
            entry_signal = True

        # Si exit está forzado (o queda true), exit manda y entry se apaga
        if exit_signal:
            entry_signal = False

    # Retornamos todas las señales y valores actuales
    return {
        "regime_on": bool(regime_on),
        "entry_signal": bool(entry_signal),
        "exit_signal": bool(exit_signal),
        "close": close,
        "sma200": sma200_v,
        "donchian_high20": high20,
        "donchian_low10": low10,
        "atr14": atr14_v,
    }