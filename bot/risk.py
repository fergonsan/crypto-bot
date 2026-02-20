def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def position_size_usdc(equity_usdc: float, risk_pct: float, atr14: float, entry_price: float):
    """
    Stop = entry - 2*ATR
    riesgo monetario = equity * risk_pct
    qty = risk / (2*ATR)
    """
    risk_usdc = equity_usdc * risk_pct
    stop_distance = 2.0 * atr14
    if stop_distance <= 0:
        return 0.0
    qty = risk_usdc / stop_distance
    # qty en "coin" (BTC o ETH)
    return qty
