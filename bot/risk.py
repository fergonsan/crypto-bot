"""
RISK (V3)
=========
qty = (equity * risk_pct) / (hard_stop_atr_mult * ATR)
"""

def position_size_usdc(
    equity_usdc: float,
    risk_pct: float,
    atr14: float,
    entry_price: float,
    hard_stop_atr_mult: float = 1.5,
) -> float:
    if equity_usdc <= 0 or risk_pct <= 0:
        return 0.0
    if atr14 is None or atr14 <= 0:
        return 0.0
    if entry_price is None or entry_price <= 0:
        return 0.0
    if hard_stop_atr_mult <= 0:
        return 0.0

    risk_usdc = equity_usdc * risk_pct
    stop_distance = hard_stop_atr_mult * atr14
    if stop_distance <= 0:
        return 0.0

    return float(max(0.0, risk_usdc / stop_distance))