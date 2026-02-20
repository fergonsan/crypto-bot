import os
import pandas as pd
import streamlit as st
import psycopg

st.set_page_config(page_title="Crypto Bot Dashboard", layout="wide")

def conn():
    return psycopg.connect(os.environ["DATABASE_URL"])

st.title("Crypto Bot Dashboard")

with conn() as c:
    equity = pd.read_sql("SELECT day, equity_usdc FROM equity_snapshots ORDER BY day", c)
    trades = pd.read_sql("SELECT created_at, symbol, side, qty, price, notional, reason FROM trades ORDER BY created_at DESC LIMIT 200", c)
    signals = pd.read_sql("SELECT day, symbol, regime_on, entry_signal, exit_signal, close, sma200 FROM signals ORDER BY day DESC, symbol", c)
    settings = pd.read_sql("SELECT key, value FROM settings ORDER BY key", c)
    runs = pd.read_sql("SELECT started_at, finished_at, status, message FROM bot_runs ORDER BY started_at DESC LIMIT 50", c)

c1, c2 = st.columns(2)
with c1:
    st.subheader("Equity")
    st.line_chart(equity.set_index("day")["equity_usdc"])
with c2:
    st.subheader("Settings")
    st.dataframe(settings, use_container_width=True)

st.subheader("Latest signals")
st.dataframe(signals.head(10), use_container_width=True)

st.subheader("Trades (last 200)")
st.dataframe(trades, use_container_width=True)

st.subheader("Bot runs")
st.dataframe(runs, use_container_width=True)
