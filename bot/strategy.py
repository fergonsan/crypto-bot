"""
STRATEGY (V3)
=============
- Donchian: entry=55, exit=20 (env DONCH_ENTRY/DONCH_EXIT)
- Regime: SMA50 > SMA200
- Entry: close > DonchianHigh(entry) and regime_on
- Exit:  close < DonchianLow(exit) or regime_off
"""

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


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    try:
        return int(v) if v is not None else default
    except Exception:
        return default


def _env_flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() == "true"


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    donch_entry = _env_int("DONCH_ENTRY", 55)
    donch_exit = _env_int("DONCH_EXIT", 20)

    df["sma50"] = sma(df["close"], 50)
    df["sma200"] = sma(df["close"], 200)

    df["donchian_high"] = df["high"].shift(1).rolling(donch_entry).max()
    df["donchian_low"] = df["low"].shift(1).rolling(donch_exit).min()

    # Legacy (para no romper tu tabla signals actual)
    df["donchian_high20"] = df["high"].shift(1).rolling(20).max()
    df["donchian_low10"] = df["low"].shift(1).rolling(10).min()

    df["atr14"] = atr(df, 14)
    return df


def decide(df: pd.DataFrame, symbol: str | None = None):
    row = df.iloc[-1]

    close = float(row["close"]) if pd.notna(row["close"]) else None
    sma50_v = float(row["sma50"]) if pd.notna(row["sma50"]) else None
    sma200_v = float(row["sma200"]) if pd.notna(row["sma200"]) else None

    highN = float(row["donchian_high"]) if pd.notna(row["donchian_high"]) else None
    lowM = float(row["donchian_low"]) if pd.notna(row["donchian_low"]) else None

    high20 = float(row["donchian_high20"]) if pd.notna(row["donchian_high20"]) else None
    low10 = float(row["donchian_low10"]) if pd.notna(row["donchian_low10"]) else None

    atr14_v = float(row["atr14"]) if pd.notna(row["atr14"]) else None

    if sma50_v is None or sma200_v is None:
        regime_on = False
    else:
        regime_on = sma50_v > sma200_v

    entry_signal = bool(regime_on and (highN is not None) and (close is not None) and (close > highN))
    exit_signal = bool(((lowM is not None) and (close is not None) and (close < lowM)) or (not regime_on))

    # TEST MODE (igual que antes)
    test_mode = _env_flag("TEST_MODE", "false")
    if test_mode:
        force_regime = _env_flag("TEST_FORCE_REGIME_ON", "false")
        ignore_exit = _env_flag("TEST_IGNORE_EXIT", "false")
        force_entry_sym = os.environ.get("TEST_FORCE_ENTRY_SYMBOL", "").strip()
        force_exit_sym = os.environ.get("TEST_FORCE_EXIT_SYMBOL", "").strip()

        if force_regime:
            regime_on = True
        if not regime_on:
            entry_signal = False

        if symbol and force_exit_sym and symbol == force_exit_sym:
            exit_signal = True

        if symbol and force_entry_sym and symbol == force_entry_sym:
            if ignore_exit:
                exit_signal = False
            entry_signal = True

        if exit_signal:
            entry_signal = False

    return {
        "regime_on": bool(regime_on),
        "entry_signal": bool(entry_signal),
        "exit_signal": bool(exit_signal),

        "close": close,
        "sma50": sma50_v,
        "sma200": sma200_v,

        "donchian_high": highN,
        "donchian_low": lowM,

        # legacy for DB insert
        "donchian_high20": high20,
        "donchian_low10": low10,

        "atr14": atr14_v,
        "donch_entry": _env_int("DONCH_ENTRY", 55),
        "donch_exit": _env_int("DONCH_EXIT", 20),
    }