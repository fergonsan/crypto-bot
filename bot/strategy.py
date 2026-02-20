import os
import pandas as pd


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


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["sma200"] = sma(df["close"], 200)
    df["donchian_high20"] = df["high"].shift(1).rolling(20).max()
    df["donchian_low10"] = df["low"].shift(1).rolling(10).min()
    df["atr14"] = atr(df, 14)
    return df


def _env_flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() == "true"


def decide(df: pd.DataFrame, symbol: str | None = None):
    """
    Devuelve dict con:
      regime_on, entry_signal, exit_signal y valores actuales

    MODO TEST (por env vars):
      TEST_MODE=true habilita el harness.
      TEST_FORCE_REGIME_ON=true fuerza regime_on=True (ignora SMA200).
      TEST_FORCE_ENTRY_SYMBOL="BTC/USDC" fuerza entry_signal=True para ese símbolo.
      TEST_FORCE_EXIT_SYMBOL="BTC/USDC" fuerza exit_signal=True para ese símbolo.
    """
    row = df.iloc[-1]

    close = float(row["close"]) if pd.notna(row["close"]) else None
    sma200_v = float(row["sma200"]) if pd.notna(row["sma200"]) else None
    high20 = float(row["donchian_high20"]) if pd.notna(row["donchian_high20"]) else None
    low10 = float(row["donchian_low10"]) if pd.notna(row["donchian_low10"]) else None
    atr14_v = float(row["atr14"]) if pd.notna(row["atr14"]) else None

    # Regime normal
    if sma200_v is None or close is None:
        regime_on = False
    else:
        regime_on = close > sma200_v

    # Señales normales
    entry_signal = bool(regime_on and (high20 is not None) and (close is not None) and (close > high20))
    exit_signal = bool(((low10 is not None) and (close is not None) and (close < low10)) or (not regime_on))

    # ---- TEST HARNESS ----
    test_mode = _env_flag("TEST_MODE", "false")
    if test_mode:
        force_regime = _env_flag("TEST_FORCE_REGIME_ON", "false")
        force_entry_sym = os.environ.get("TEST_FORCE_ENTRY_SYMBOL", "").strip()
        force_exit_sym = os.environ.get("TEST_FORCE_EXIT_SYMBOL", "").strip()

        if force_regime:
            regime_on = True

        # Si régimen OFF, entry debe ser False
        if not regime_on:
            entry_signal = False

        if symbol and force_entry_sym and symbol == force_entry_sym:
            entry_signal = True

        if symbol and force_exit_sym and symbol == force_exit_sym:
            exit_signal = True

        # Si forzamos exit, exit manda
        if exit_signal:
            entry_signal = False

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