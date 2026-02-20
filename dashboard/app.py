import os
import pandas as pd
import psycopg
import streamlit as st
import plotly.express as px

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
    dd = (equity / peak) - 1.0
    return dd


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
auto_refresh = st.sidebar.toggle("Auto-refresh (cada 30s)", value=True)
if auto_refresh:
    st.cache_data.clear()

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
colA, colB, colC, colD, colE = st.columns(5)

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

n_trades = len(trades)
n_buys = int((trades["side"] == "buy").sum()) if n_trades else 0
n_sells = int((trades["side"] == "sell").sum()) if n_trades else 0

kpi_block("Equity actual (USDC)", f"{eqN:,.2f}")
kpi_block("Retorno ventana", f"{ret*100:,.2f}%")
kpi_block("Max Drawdown", f"{mdd*100:,.2f}%")
kpi_block("Trades (ventana)", f"{n_trades}")
kpi_block("BUY/SELL", f"{n_buys}/{n_sells}")

st.divider()

# -----------------------
# Charts: Equity + Drawdown
# -----------------------
c1, c2 = st.columns([2, 1])

with c1:
    st.subheader("Equity curve")
    if equity.empty:
        st.info("Sin datos en equity_snapshots aún.")
    else:
        fig = px.line(equity, x="day", y="equity_usdc")
        fig.update_layout(height=360, margin=dict(l=10, r=10, t=40, b=10))
        st.plotly_chart(fig, use_container_width=True)

with c2:
    st.subheader("Drawdown")
    if equity.empty:
        st.info("Sin datos.")
    else:
        dd = equity.copy()
        dd["drawdown"] = compute_drawdown(dd["equity_usdc"])
        fig = px.area(dd, x="day", y="drawdown")
        fig.update_layout(height=360, margin=dict(l=10, r=10, t=40, b=10))
        st.plotly_chart(fig, use_container_width=True)

st.divider()

# -----------------------
# Positions + Signals snapshot
# -----------------------
c3, c4 = st.columns([1, 2])

with c3:
    st.subheader("Positions (BOT)")
    if positions_f.empty:
        st.info("Sin posiciones.")
    else:
        st.dataframe(positions_f, use_container_width=True, hide_index=True)

with c4:
    st.subheader("Últimas señales")
    if signals.empty:
        st.info("Sin señales.")
    else:
        # last day per symbol
        latest_day = signals["day"].max()
        latest = signals[signals["day"] == latest_day].copy()
        latest = latest.sort_values("symbol")
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

st.caption("Tip: si ves 'DISABLED' en runs, trading_enabled=false. Si ves trades pero no cambian posiciones, hay bug en set_bot_position.")