import os
from dataclasses import dataclass
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


# -----------------------
# DB helpers
# -----------------------
@st.cache_data(ttl=30)
def read_df(query: str, params=None) -> pd.DataFrame:
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            cols = [c.name for c in cur.description]
            rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


def to_float_series(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").astype(float)


def compute_drawdown(equity: pd.Series) -> pd.Series:
    eq = pd.to_numeric(equity, errors="coerce").astype(float)
    peak = eq.cummax()
    return (eq / peak) - 1.0


# -----------------------
# FX
# -----------------------
@st.cache_data(ttl=300)
def get_usdc_to_eur_rate() -> tuple[float, str]:
    env_fx = os.environ.get("FX_USDC_EUR")
    if env_fx:
        try:
            v = float(env_fx)
            if v > 0:
                return v, "env(FX_USDC_EUR)"
        except Exception:
            pass

    ex = ccxt.binance({"enableRateLimit": True})
    candidates = [
        ("USDC/EUR", "direct"),
        ("EUR/USDC", "inverse"),
        ("USDT/EUR", "direct"),
        ("EUR/USDT", "inverse"),
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
            if mode == "direct":
                return last, f"binance:{sym}"
            return 1.0 / last, f"binance:{sym} (inverted)"
        except Exception:
            continue

    return 0.92, "fallback(0.92)"


# -----------------------
# Trade pairing + metrics
# -----------------------
@dataclass
class RoundTrip:
    symbol: str
    buy_id: int
    sell_id: int
    buy_time: pd.Timestamp
    sell_time: pd.Timestamp
    qty: float
    buy_price: float
    sell_price: float
    buy_notional: float
    sell_notional: float
    gross_pnl: float
    net_pnl: float
    return_pct: float
    holding_hours: float


def pair_round_trips(trades: pd.DataFrame, fee_bps: float, slippage_bps: float) -> pd.DataFrame:
    """
    Empareja BUY->SELL por símbolo (FIFO) suponiendo que el bot opera 'flat->in->flat' (una posición por símbolo).
    Calcula PnL con:
      - fee por cada lado: fee_bps (bps sobre notional)
      - slippage por cada lado: slippage_bps (bps sobre precio)
    """
    if trades.empty:
        return pd.DataFrame()

    t = trades.copy()
    t["created_at"] = pd.to_datetime(t["created_at"], utc=True, errors="coerce")
    t["qty"] = to_float_series(t["qty"])
    t["price"] = to_float_series(t["price"])
    t["notional"] = to_float_series(t["notional"])
    t = t.sort_values(["symbol", "created_at", "id"])

    fee_rate = fee_bps / 10_000.0
    slip_rate = slippage_bps / 10_000.0

    rows: list[RoundTrip] = []
    open_buys: dict[str, dict] = {}

    for _, r in t.iterrows():
        sym = str(r["symbol"])
        side = str(r["side"]).lower()

        if side == "buy":
            # si ya hay buy abierto, lo ignoramos (no debería pasar si estrategia flat->in->flat)
            open_buys[sym] = {
                "id": int(r["id"]),
                "time": r["created_at"],
                "qty": float(r["qty"]),
                "price": float(r["price"]),
                "notional": float(r["notional"]),
            }
        elif side == "sell":
            if sym not in open_buys:
                continue

            b = open_buys.pop(sym)
            qty = min(float(b["qty"]), float(r["qty"])) if float(r["qty"]) > 0 else float(b["qty"])

            buy_price = float(b["price"]) * (1.0 + slip_rate)   # peor para nosotros
            sell_price = float(r["price"]) * (1.0 - slip_rate)  # peor para nosotros

            buy_notional = qty * buy_price
            sell_notional = qty * sell_price

            gross_pnl = sell_notional - buy_notional
            fees = (buy_notional + sell_notional) * fee_rate
            net_pnl = gross_pnl - fees

            ret_pct = (net_pnl / buy_notional) if buy_notional > 0 else 0.0
            holding_hours = (pd.Timestamp(r["created_at"]) - pd.Timestamp(b["time"])).total_seconds() / 3600.0

            rows.append(
                RoundTrip(
                    symbol=sym,
                    buy_id=int(b["id"]),
                    sell_id=int(r["id"]),
                    buy_time=pd.Timestamp(b["time"]),
                    sell_time=pd.Timestamp(r["created_at"]),
                    qty=qty,
                    buy_price=buy_price,
                    sell_price=sell_price,
                    buy_notional=buy_notional,
                    sell_notional=sell_notional,
                    gross_pnl=gross_pnl,
                    net_pnl=net_pnl,
                    return_pct=ret_pct,
                    holding_hours=holding_hours,
                )
            )

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame([rt.__dict__ for rt in rows])
    df = df.sort_values("sell_time", ascending=False)
    return df


def perf_summary(roundtrips: pd.DataFrame) -> dict:
    if roundtrips.empty:
        return {
            "n": 0,
            "winrate": 0.0,
            "gross": 0.0,
            "net": 0.0,
            "avg_net": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "profit_factor": 0.0,
            "expectancy": 0.0,
            "avg_hold_h": 0.0,
        }

    n = len(roundtrips)
    wins = roundtrips[roundtrips["net_pnl"] > 0]
    losses = roundtrips[roundtrips["net_pnl"] < 0]

    winrate = len(wins) / n if n else 0.0
    gross = float(roundtrips["gross_pnl"].sum())
    net = float(roundtrips["net_pnl"].sum())
    avg_net = float(roundtrips["net_pnl"].mean())
    avg_win = float(wins["net_pnl"].mean()) if len(wins) else 0.0
    avg_loss = float(losses["net_pnl"].mean()) if len(losses) else 0.0  # negativo

    gain = float(wins["net_pnl"].sum()) if len(wins) else 0.0
    pain = float((-losses["net_pnl"]).sum()) if len(losses) else 0.0
    profit_factor = (gain / pain) if pain > 0 else (float("inf") if gain > 0 else 0.0)

    # expectancy por trade
    expectancy = (winrate * avg_win) + ((1.0 - winrate) * avg_loss)

    avg_hold_h = float(roundtrips["holding_hours"].mean()) if "holding_hours" in roundtrips else 0.0

    return {
        "n": n,
        "winrate": winrate,
        "gross": gross,
        "net": net,
        "avg_net": avg_net,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "avg_hold_h": avg_hold_h,
    }


# -----------------------
# UI
# -----------------------
st.title("📈 Crypto Bot Dashboard")

st.sidebar.header("Filtros")
days = st.sidebar.selectbox("Ventana (días)", [7, 14, 30, 60, 90, 180, 365], index=2)

symbols = read_df("SELECT DISTINCT symbol FROM signals ORDER BY symbol")["symbol"].tolist()
symbols_sel = st.sidebar.multiselect("Símbolos", symbols, default=symbols)

st.sidebar.divider()
show_eur = st.sidebar.toggle("Mostrar EUR aproximado", value=True)

st.sidebar.subheader("Costes (estimación)")
fee_bps = st.sidebar.number_input("Fee total por lado (bps)", min_value=0.0, max_value=50.0, value=10.0, step=1.0)
slippage_bps = st.sidebar.number_input("Slippage por lado (bps)", min_value=0.0, max_value=100.0, value=5.0, step=1.0)
st.sidebar.caption("Ejemplo: 10 bps = 0.10% por lado. En cripto spot suele ser bajo, pero mejor ser conservador.")

st.sidebar.divider()
auto_refresh = st.sidebar.toggle("Auto-refresh (cada 30s)", value=True)
if auto_refresh:
    st.cache_data.clear()

eur_per_usdc, fx_src = get_usdc_to_eur_rate()

# -----------------------
# Load tables
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
    ORDER BY created_at ASC
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

# Types fix
if not equity.empty:
    equity["equity_usdc"] = to_float_series(equity["equity_usdc"])

# Filter by symbols
signals = signals[signals["symbol"].isin(symbols_sel)]
trades = trades[trades["symbol"].isin(symbols_sel)] if len(symbols_sel) else trades
positions_f = positions[positions["symbol"].isin(symbols_sel)] if len(symbols_sel) else positions

# Round trips
roundtrips = pair_round_trips(trades, fee_bps=fee_bps, slippage_bps=slippage_bps)
summary = perf_summary(roundtrips)

# -----------------------
# KPIs
# -----------------------
c1, c2, c3, c4, c5, c6 = st.columns(6)

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

c1.metric("Equity (USDC)", f"{eqN:,.2f}")
c2.metric("Equity (EUR aprox)", f"{eqN_eur:,.2f}", help=f"1 USDC ≈ {eur_per_usdc:.6f} EUR · {fx_src}" if show_eur else None)
c3.metric("Retorno ventana", f"{ret*100:,.2f}%")
c4.metric("Max Drawdown", f"{mdd*100:,.2f}%")
c5.metric("Round-trips", f"{summary['n']}")
c6.metric("Winrate", f"{summary['winrate']*100:,.1f}%")

if show_eur:
    st.caption(f"Conversión aprox: 1 USDC ≈ {eur_per_usdc:.6f} EUR · Fuente: {fx_src}")

st.divider()

# -----------------------
# Performance block
# -----------------------
p1, p2, p3, p4, p5 = st.columns(5)
p1.metric("Net PnL (estim.)", f"{summary['net']:,.2f} USDC")
p2.metric("Gross PnL", f"{summary['gross']:,.2f} USDC")
p3.metric("Profit Factor", "∞" if summary["profit_factor"] == float("inf") else f"{summary['profit_factor']:,.2f}")
p4.metric("Expectancy / trade", f"{summary['expectancy']:,.3f} USDC", help="E[PNL] por round-trip, neto (fees+slippage estimados)")
p5.metric("Holding medio", f"{summary['avg_hold_h']:,.1f} h")

st.divider()

# -----------------------
# Charts: Equity + Drawdown
# -----------------------
a, b = st.columns([2, 1])

with a:
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
        fig.update_layout(height=340, margin=dict(l=10, r=10, t=40, b=10))
        st.plotly_chart(fig, use_container_width=True)

with b:
    st.subheader("Drawdown")
    if equity.empty:
        st.info("Sin datos.")
    else:
        dd_df = equity.copy()
        dd_df["drawdown"] = compute_drawdown(dd_df["equity_usdc"])
        fig = px.area(dd_df, x="day", y="drawdown", labels={"drawdown": "drawdown", "day": "día"})
        fig.update_layout(height=340, margin=dict(l=10, r=10, t=40, b=10))
        st.plotly_chart(fig, use_container_width=True)

st.divider()

# -----------------------
# Round-trips table + PnL chart
# -----------------------
rt1, rt2 = st.columns([2, 1])

with rt1:
    st.subheader("Round-trips (BUY→SELL) con PnL estimado")
    if roundtrips.empty:
        st.info("Aún no hay round-trips completos en la ventana (necesitas BUY y SELL).")
    else:
        show_cols = [
            "sell_time", "symbol", "qty", "buy_price", "sell_price",
            "buy_notional", "sell_notional", "gross_pnl", "net_pnl", "return_pct", "holding_hours"
        ]
        df_show = roundtrips.copy()
        df_show["return_pct"] = df_show["return_pct"] * 100.0
        st.dataframe(df_show[show_cols], use_container_width=True, hide_index=True)

with rt2:
    st.subheader("PNL neto por round-trip")
    if roundtrips.empty:
        st.info("Sin datos.")
    else:
        pnl_plot = roundtrips.copy().sort_values("sell_time")
        fig = px.bar(pnl_plot, x="sell_time", y="net_pnl")
        fig.update_layout(height=320, margin=dict(l=10, r=10, t=40, b=10))
        st.plotly_chart(fig, use_container_width=True)

st.divider()

# -----------------------
# Positions + Signals snapshot
# -----------------------
c7, c8 = st.columns([1, 2])

with c7:
    st.subheader("Positions (BOT)")
    if positions_f.empty or (to_float_series(positions_f["qty"]).abs().sum() == 0):
        st.info("Sin posiciones (qty=0).")
    else:
        st.dataframe(positions_f, use_container_width=True, hide_index=True)

with c8:
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
# Raw trades + Bot runs + Settings
# -----------------------
t1, t2 = st.columns([2, 1])

with t1:
    st.subheader("Trades (raw)")
    if trades.empty:
        st.info("Sin trades en la ventana.")
    else:
        # mostrar desc por lectura humana
        st.dataframe(trades.sort_values("created_at", ascending=False), use_container_width=True, hide_index=True)

with t2:
    st.subheader("Settings")
    st.dataframe(settings, use_container_width=True, hide_index=True)

st.subheader("Bot runs (últimos 50)")
st.dataframe(bot_runs, use_container_width=True, hide_index=True)

st.caption("Notas: PnL es estimado (fee+slippage configurables). Emparejado BUY→SELL por símbolo (asume flat→in→flat).")