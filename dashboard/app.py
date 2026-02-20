import os
import pandas as pd
import psycopg
import streamlit as st
import plotly.express as px
import ccxt

st.set_page_config(page_title="Crypto Bot Dashboard", layout="wide")

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    st.error("Falta DATABASE_URL en variables de entorno.")
    st.stop()


@st.cache_data(ttl=30)
def read_df(query: str, params=None) -> pd.DataFrame:
    with psycopg.connect(DATABASE_URL) as conn:
        return pd.read_sql(query, conn, params=params)


def compute_drawdown(equity: pd.Series) -> pd.Series:
    peak = equity.cummax()
    return (equity / peak) - 1.0


@st.cache_data(ttl=300)  # 5 min
def get_usdc_to_eur_rate() -> tuple[float, str]:
    """
    Devuelve (eur_per_usdc, source)
    Prioridad:
      1) env FX_USDC_EUR (manual override)
      2) Binance via ccxt (public)
      3) fallback 0.92
    """
    env_fx = os.environ.get("FX_USDC_EUR")
    if env_fx:
        try:
            v = float(env_fx)
            if v > 0:
                return v, "env(FX_USDC_EUR)"
        except Exception:
            pass

    ex = ccxt.binance({"enableRateLimit": True})

    # intentamos pares directos/inversos
    candidates = [
        ("USDC/EUR", "direct_usdc_eur"),     # price = EUR por 1 USDC
        ("EUR/USDC", "inverse_eur_usdc"),    # price = USDC por 1 EUR => eur/usdc = 1/price
        ("USDT/EUR", "direct_usdt_eur"),     # approx USDC
        ("EUR/USDT", "inverse_eur_usdt"),    # eur/usdc approx 1/price
    ]

    for sym, mode in candidates:
        try:
            t = ex.fetch_ticker(sym)
            last = t.get("last")
            if not last:
                continue
            last = float(last)
            if last <= 0:
                continue

            if mode.startswith("direct"):
                # last ya es EUR por 1 USDC/USDT
                return last, f"binance:{sym}"
            else:
                # last es USDC/USDT por 1 EUR => eur/usdc = 1/last
                return 1.0 / last, f"binance:{sym} (inverted)"
        except Exception:
            continue

    return 0.92, "fallback(0.92)"


def kpi_block(label: str, value: str, help_text: str | None = None):
    st.metric(label, value, help=help_text)


st.title("📈 Crypto Bot Dashboard")

# -----------------------
# Sidebar controls
# -----------------------
st.sidebar.header("Filtros")
days = st.sidebar.selectbox("Ventana (días)", [7, 14, 30, 60, 90, 180, 365], index=2)

symbols = read_df("SELECT DISTINCT symbol FROM signals ORDER BY symbol")["symbol"].tolist()
symbols_sel = st.sidebar.multiselect("Símbolos", symbols, default=symbols)

st.sidebar.divider()
show_eur = st.sidebar.toggle("Mostrar EUR aproximado", value=True)

auto_refresh = st.sidebar.toggle("Auto-refresh (cada 30s)", value=True)
if auto_refresh:
    st.cache_data.clear()

# -----------------------
# FX
# -----------------------
eur_per_usdc, fx_src = get_usdc_to_eur_rate()

# -----------------------
# Load core tables
# -----------------------
equity = read_df(
    """
    SELECT day, equity_usdc
    FROM equity_snapshots
    WHERE day >= CURRENT_DATE - (%s::int)
    ORDER BY day
    """,
    params=(days,),
)

trades = read_df(
    """
    SELECT id, created_at, symbol, side, qty, price, notional, reason
    FROM trades
    WHERE created_at >= NOW() - (%s::int || ' days')::interval
    ORDER BY created_at DESC
    """,
    params=(days,),
)

positions = read_df(
    """
    SELECT symbol, qty, avg_price, updated_at
    FROM positions
    ORDER BY symbol
    """
)

signals = read_df(
    """
    SELECT day, symbol, regime_on, entry_signal, exit_signal, close, sma200, donchian_high20, donchian_low10, atr14
    FROM signals
    WHERE day >= CURRENT_DATE - (%s::int)
    ORDER BY day DESC, symbol
    """,
    params=(days,),
)

bot_runs = read_df(
    """
    SELECT id, started_at, finished_at, status, message
    FROM bot_runs
    ORDER BY id DESC
    LIMIT 50
    """
)

settings = read_df("SELECT key, value FROM settings ORDER BY key")

# Filter by symbols
signals = signals[signals["symbol"].isin(symbols_sel)]
trades = trades[trades["symbol"].isin(symbols_sel)] if len(symbols_sel) else trades
positions_f = positions[positions["symbol"].isin(symbols_sel)] if len(symbols_sel) else positions

# -----------------------
# KPIs
# -----------------------
colA, colB, colC, colD, colE, colF = st.columns(6)

if not equity.empty:
    eq0 = float(equity["equity_usdc"].iloc[0])
    eqN = float(equity["equity_usdc"].iloc[-1])
    ret = (eqN / eq0 - 1.0) if eq0 > 0 else 0.0
    dd = compute_drawdown(equity["equity_usdc"])
    mdd = float(dd.min()) if not dd.empty else 0.0
else:
    eqN = 0.0
    ret = 0.0
    mdd = 0.0

eqN_eur = eqN * eur_per_usdc

n_trades = len(trades)
n_buys = int((trades["side"] == "buy").sum()) if n_trades else 0
n_sells = int((trades["side"] == "sell").sum()) if n_trades else 0

kpi_block("Equity actual (USDC)", f"{eqN:,.2f}")
kpi_block("Equity aprox (EUR)", f"{eqN_eur:,.2f}", help_text=f"FX: {eur_per_usdc:.6f} EUR/USDC ({fx_src})" if show_eur else None)
kpi_block("Retorno ventana", f"{ret*100:,.2f}%")
kpi_block("Max Drawdown", f"{mdd*100:,.2f}%")
kpi_block("Trades (ventana)", f"{n_trades}")
kpi_block("BUY/SELL", f"{n_buys}/{n_sells}")

if show_eur:
    st.caption(f"Conversión aprox: 1 USDC ≈ {eur_per_usdc:.6f} EUR  ·  Fuente: {fx_src}")

st.divider()

# -----------------------
# Charts: Equity + Drawdown
# -----------------------
c1, c2 = st.columns([2, 1])

with c1:
    st.subheader("Equity curve")
    if equity.empty:
        st.info("Sin datos en equity_snapshots aún. Ejecuta el bot al menos una vez tras el reset.")
    else:
        eq_plot = equity.copy()
        metric = st.radio("Unidad", ["USDC", "EUR (aprox)"] if show_eur else ["USDC"], horizontal=True)
        if metric.startswith("EUR"):
            eq_plot["equity"] = eq_plot["equity_usdc"] * eur_per_usdc
            ycol = "equity"
            ytitle = "equity (EUR aprox)"
        else:
            ycol = "equity_usdc"
            ytitle = "equity (USDC)"

        fig = px.line(eq_plot, x="day", y=ycol, labels={ycol: ytitle, "day": "día"})
        fig.update_layout(height=360, margin=dict(l=10, r=10, t=40, b=10))
        st.plotly_chart(fig, use_container_width=True)

with c2:
    st.subheader("Drawdown")
    if equity.empty:
        st.info("Sin datos.")
    else:
        dd_df = equity.copy()
        dd_df["drawdown"] = compute_drawdown(dd_df["equity_usdc"])
        fig = px.area(dd_df, x="day", y="drawdown", labels={"drawdown": "drawdown", "day": "día"})
        fig.update_layout(height=360, margin=dict(l=10, r=10, t=40, b=10))
        st.plotly_chart(fig, use_container_width=True)

st.divider()

# -----------------------
# Positions + Signals snapshot
# -----------------------
c3, c4 = st.columns([1, 2])

with c3:
    st.subheader("Positions (BOT)")
    if positions_f.empty or (positions_f["qty"].fillna(0).abs().sum() == 0):
        st.info("Sin posiciones (qty=0).")
    else:
        st.dataframe(positions_f, use_container_width=True, hide_index=True)

with c4:
    st.subheader("Últimas señales")
    if signals.empty:
        st.info("Sin señales.")
    else:
        latest_day = signals["day"].max()
        latest = signals[signals["day"] == latest_day].copy().sort_values("symbol")
        st.caption(f"Día: {latest_day}")
        st.dataframe(
            latest[["symbol", "regime_on", "entry_signal", "exit_signal", "close", "sma200", "atr14"]],
            use_container_width=True,
            hide_index=True,
        )

st.divider()

# -----------------------
# Trades table
# -----------------------
st.subheader("Trades (ventana)")
if trades.empty:
    st.info("Sin trades en la ventana seleccionada.")
else:
    st.dataframe(trades, use_container_width=True, hide_index=True)

st.divider()

# -----------------------
# Bot runs + Settings
# -----------------------
c5, c6 = st.columns([2, 1])

with c5:
    st.subheader("Bot runs (últimos 50)")
    st.dataframe(bot_runs, use_container_width=True, hide_index=True)

with c6:
    st.subheader("Settings")
    st.dataframe(settings, use_container_width=True, hide_index=True)

st.caption("Tip: si ves trades pero positions no cambia, revisa set_bot_position. Si ves DISABLED, trading_enabled=false.")