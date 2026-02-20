import pandas as pd
import numpy as np

def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n).mean()

def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        (df["high"] - df["low"]),
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr

def atr(df: pd.DataFrame, n: int) -> pd.Series:
    return true_range(df).rolling(n).mean()

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["sma200"] = sma(df["close"], 200)
    df["donchian_high20"] = df["high"].shift(1).rolling(20).max()  # últimos 20 días previos
    df["donchian_low10"]  = df["low"].shift(1).rolling(10).min()
    df["atr14"] = atr(df, 14)
    return df

def decide(df: pd.DataFrame):
    """
    Devuelve dict con:
      regime_on, entry_signal, exit_signal y valores actuales
    """
    row = df.iloc[-1]
    regime_on = (row["close"] > row["sma200"]) if pd.notna(row["sma200"]) else False

    entry_signal = regime_on and pd.notna(row["donchian_high20"]) and (row["close"] > row["donchian_high20"])
    exit_signal = (pd.notna(row["donchian_low10"]) and (row["close"] < row["donchian_low10"])) or (not regime_on)

    return {
        "regime_on": bool(regime_on),
        "entry_signal": bool(entry_signal),
        "exit_signal": bool(exit_signal),
        "close": float(row["close"]) if pd.notna(row["close"]) else None,
        "sma200": float(row["sma200"]) if pd.notna(row["sma200"]) else None,
        "donchian_high20": float(row["donchian_high20"]) if pd.notna(row["donchian_high20"]) else None,
        "donchian_low10": float(row["donchian_low10"]) if pd.notna(row["donchian_low10"]) else None,
        "atr14": float(row["atr14"]) if pd.notna(row["atr14"]) else None,
    }
