"""
MÓDULO DE GESTIÓN DE RIESGO
============================

Este módulo contiene funciones para calcular el tamaño de posición
basado en gestión de riesgo.

La estrategia actual usa:
- Riesgo: 1% del equity por operación
- Stop loss: 2 * ATR por debajo del precio de entrada
- Tamaño de posición calculado para que si el stop se activa, solo perdamos el % de riesgo definido
"""

def clamp(x, lo, hi):
    """
    Función auxiliar para limitar un valor entre un mínimo y máximo.
    
    Args:
        x: Valor a limitar
        lo: Valor mínimo
        hi: Valor máximo
    
    Returns:
        Valor limitado entre lo y hi
    """
    return max(lo, min(hi, x))

def position_size_usdc(equity_usdc: float, risk_pct: float, atr14: float, entry_price: float):
    """
    ⚠️ FUNCIÓN PRINCIPAL DE CÁLCULO DE TAMAÑO DE POSICIÓN ⚠️
    
    Calcula cuánto comprar basándose en gestión de riesgo.
    
    LÓGICA:
    - Stop loss = precio_entrada - (2 * ATR)
    - Riesgo monetario = equity * risk_pct (ej: 1% = 0.01)
    - Cantidad = riesgo_monetario / distancia_al_stop
    
    Ejemplo:
    - Equity: 1000 USDC
    - Riesgo: 1% = 10 USDC
    - ATR: 50 USDC
    - Stop distance: 2 * 50 = 100 USDC
    - Cantidad: 10 / 100 = 0.1 BTC (si BTC cuesta 1000 USDC)
    
    ⚠️ MODIFICAR AQUÍ: Cambia el multiplicador del ATR (actualmente 2.0) o la lógica de riesgo
    
    Args:
        equity_usdc: Equity total del bot en USDC
        risk_pct: Porcentaje de riesgo por operación (ej: 0.01 = 1%)
        atr14: Average True Range de 14 períodos
        entry_price: Precio de entrada esperado
    
    Returns:
        Cantidad del activo a comprar (en unidades del activo, ej: BTC, ETH)
    """
    # Calcular cuánto dinero estamos dispuestos a arriesgar
    risk_usdc = equity_usdc * risk_pct
    
    # Distancia al stop loss = 2 * ATR
    # ⚠️ MODIFICAR AQUÍ: Cambia 2.0 por otro multiplicador si quieres stops más cercanos/lejanos
    stop_distance = 2.0 * atr14
    
    if stop_distance <= 0:
        return 0.0
    
    # Calcular cantidad: riesgo / distancia_al_stop
    qty = risk_usdc / stop_distance
    
    # Retornamos cantidad en unidades del activo (BTC, ETH, etc.)
    return qty
