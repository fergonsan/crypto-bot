import os
from dataclasses import dataclass
import pandas as pd
import psycopg
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
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
            buy_price = float(b["price"]) * (1.0 + slip_rate)
            sell_price = float(r["price"]) * (1.0 - slip_rate)
            buy_notional = qty * buy_price
            sell_notional = qty * sell_price
            gross_pnl = sell_notional - buy_notional
            fees = (buy_notional + sell_notional) * fee_rate
            net_pnl = gross_pnl - fees
            ret_pct = (net_pnl / buy_notional) if buy_notional > 0 else 0.0
            holding_hours = (
                pd.Timestamp(r["created_at"]) - pd.Timestamp(b["time"])
            ).total_seconds() / 3600.0
            rows.append(RoundTrip(
                symbol=sym, buy_id=int(b["id"]), sell_id=int(r["id"]),
                buy_time=pd.Timestamp(b["time"]), sell_time=pd.Timestamp(r["created_at"]),
                qty=qty, buy_price=buy_price, sell_price=sell_price,
                buy_notional=buy_notional, sell_notional=sell_notional,
                gross_pnl=gross_pnl, net_pnl=net_pnl,
                return_pct=ret_pct, holding_hours=holding_hours,
            ))

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([rt.__dict__ for rt in rows])
    return df.sort_values("sell_time", ascending=False)


def perf_summary(roundtrips: pd.DataFrame) -> dict:
    if roundtrips.empty:
        return {
            "n": 0, "winrate": 0.0, "gross": 0.0, "net": 0.0,
            "avg_net": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
            "profit_factor": 0.0, "expectancy": 0.0, "avg_hold_h": 0.0,
        }
    n = len(roundtrips)
    wins = roundtrips[roundtrips["net_pnl"] > 0]
    losses = roundtrips[roundtrips["net_pnl"] < 0]
    winrate = len(wins) / n if n else 0.0
    gross = float(roundtrips["gross_pnl"].sum())
    net = float(roundtrips["net_pnl"].sum())
    avg_net = float(roundtrips["net_pnl"].mean())
    avg_win = float(wins["net_pnl"].mean()) if len(wins) else 0.0
    avg_loss = float(losses["net_pnl"].mean()) if len(losses) else 0.0
    gain = float(wins["net_pnl"].sum()) if len(wins) else 0.0
    pain = float((-losses["net_pnl"]).sum()) if len(losses) else 0.0
    profit_factor = (gain / pain) if pain > 0 else (float("inf") if gain > 0 else 0.0)
    expectancy = (winrate * avg_win) + ((1.0 - winrate) * avg_loss)
    avg_hold_h = float(roundtrips["holding_hours"].mean()) if "holding_hours" in roundtrips else 0.0
    return {
        "n": n, "winrate": winrate, "gross": gross, "net": net,
        "avg_net": avg_net, "avg_win": avg_win, "avg_loss": avg_loss,
        "profit_factor": profit_factor, "expectancy": expectancy, "avg_hold_h": avg_hold_h,
    }


# -----------------------
# DIAGNÓSTICO DE SEÑALES
# -----------------------
def compute_signal_diagnosis(signals_df: pd.DataFrame) -> pd.DataFrame:
    """
    Bug 1 fix: usa donchian_high_real (Donchian real configurado) cuando existe,
    con fallback a donchian_high20 para compatibilidad con señales antiguas.
    """
    if signals_df.empty:
        return pd.DataFrame()

    df = signals_df.copy()
    df["day"] = pd.to_datetime(df["day"])
    df = df.sort_values(["symbol", "day"])

    has_real = "donchian_high_real" in df.columns
    has_donch_n = "donch_entry_n" in df.columns

    for col in ["close", "sma200", "donchian_high20", "atr14"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if has_real:
        df["donchian_high_real"] = pd.to_numeric(df["donchian_high_real"], errors="coerce")

    results = []

    for sym, grp in df.groupby("symbol"):
        grp = grp.sort_values("day").reset_index(drop=True)
        latest = grp.iloc[-1]

        close = float(latest["close"]) if pd.notna(latest.get("close")) else None
        sma200 = float(latest["sma200"]) if pd.notna(latest.get("sma200")) else None
        atr14 = float(latest["atr14"]) if pd.notna(latest.get("atr14")) else None
        regime_on = bool(latest["regime_on"]) if pd.notna(latest.get("regime_on")) else False
        entry_signal = bool(latest["entry_signal"]) if pd.notna(latest.get("entry_signal")) else False
        exit_signal = bool(latest["exit_signal"]) if pd.notna(latest.get("exit_signal")) else False

        # Bug 1 fix: Donchian real si existe, si no fallback a legacy 20p
        real_val = latest.get("donchian_high_real") if has_real else None
        if has_real and pd.notna(real_val):
            donch_high = float(real_val)
            donch_n = int(latest["donch_entry_n"]) if has_donch_n and pd.notna(latest.get("donch_entry_n")) else "?"
            donch_label = f"Donchian {donch_n}p (real)"
        else:
            donch_high = float(latest["donchian_high20"]) if pd.notna(latest.get("donchian_high20")) else None
            donch_n = 20
            donch_label = "Donchian 20p (legacy — actualiza el bot)"

        dist_to_breakout_pct = None
        if close and donch_high and donch_high > 0:
            dist_to_breakout_pct = (close / donch_high - 1.0) * 100.0

        dist_to_sma200_pct = None
        if close and sma200 and sma200 > 0:
            dist_to_sma200_pct = (close / sma200 - 1.0) * 100.0

        # Racha del estado de régimen actual
        regime_streak = 0
        for i in range(len(grp) - 1, -1, -1):
            if bool(grp.iloc[i]["regime_on"]) == regime_on:
                regime_streak += 1
            else:
                break

        # Última señal de entrada en la ventana
        entry_rows = grp[grp["entry_signal"] == True]
        last_entry_date = entry_rows["day"].max() if not entry_rows.empty else None
        days_since_last_entry = None
        if last_entry_date is not None:
            days_since_last_entry = (
                pd.Timestamp(latest["day"]) - pd.Timestamp(last_entry_date)
            ).days

        # Días cerca del breakout (≤2%) en últimos 30 días
        recent = grp.tail(30).copy()
        recent["close_f"] = pd.to_numeric(recent["close"], errors="coerce")
        if has_real:
            recent["donch_f"] = pd.to_numeric(recent.get("donchian_high_real"), errors="coerce")
            mask = recent["donch_f"].isna()
            recent.loc[mask, "donch_f"] = pd.to_numeric(
                recent.loc[mask, "donchian_high20"], errors="coerce"
            )
        else:
            recent["donch_f"] = pd.to_numeric(recent["donchian_high20"], errors="coerce")

        recent["near"] = (
            recent["close_f"].notna() & recent["donch_f"].notna() &
            (recent["donch_f"] > 0) &
            ((recent["close_f"] / recent["donch_f"]) >= 0.98)
        )
        days_near_breakout_30d = int(recent["near"].sum())

        # Histórico distancia al breakout (60 días)
        hist = grp.tail(60).copy()
        hist["close_h"] = pd.to_numeric(hist["close"], errors="coerce")
        if has_real:
            hist["donch_h"] = pd.to_numeric(hist.get("donchian_high_real"), errors="coerce")
            mask = hist["donch_h"].isna()
            hist.loc[mask, "donch_h"] = pd.to_numeric(
                hist.loc[mask, "donchian_high20"], errors="coerce"
            )
        else:
            hist["donch_h"] = pd.to_numeric(hist["donchian_high20"], errors="coerce")

        hist["dist_pct"] = (hist["close_h"] / hist["donch_h"] - 1.0) * 100.0

        results.append({
            "symbol": sym,
            "day": latest["day"],
            "close": close,
            "sma200": sma200,
            "donch_high": donch_high,
            "donch_label": donch_label,
            "atr14": atr14,
            "regime_on": regime_on,
            "entry_signal": entry_signal,
            "exit_signal": exit_signal,
            "dist_to_breakout_pct": dist_to_breakout_pct,
            "dist_to_sma200_pct": dist_to_sma200_pct,
            "regime_streak_days": regime_streak,
            "last_entry_date": last_entry_date,
            "days_since_last_entry": days_since_last_entry,
            "days_near_breakout_30d": days_near_breakout_30d,
            "_hist": hist[["day", "dist_pct"]].dropna(),
        })

    return pd.DataFrame(results)


def render_signal_diagnosis(diag_df: pd.DataFrame):
    st.subheader("🔍 Diagnóstico de señales — ¿Por qué no opera?")
    st.caption(
        "Estado actual de cada símbolo respecto a las dos condiciones de entrada: "
        "régimen (SMA50>SMA200) y breakout Donchian."
    )

    if diag_df.empty:
        st.info("Sin datos de señales disponibles.")
        return

    for _, row in diag_df.iterrows():
        sym = row["symbol"]
        regime_on = row["regime_on"]
        entry_signal = row["entry_signal"]
        dist_breakout = row["dist_to_breakout_pct"]
        dist_sma200 = row["dist_to_sma200_pct"]
        regime_streak = row["regime_streak_days"]
        days_since_entry = row["days_since_last_entry"]
        last_entry = row["last_entry_date"]
        days_near = row["days_near_breakout_30d"]
        donch_label = row["donch_label"]
        hist = row["_hist"]

        if entry_signal:
            status_icon, status_text = "🟢", "SEÑAL DE ENTRADA ACTIVA"
        elif regime_on:
            status_icon, status_text = "🟡", "Régimen ON — esperando breakout"
        else:
            status_icon, status_text = "🔴", "Régimen OFF — bot inactivo"

        with st.expander(f"{status_icon} **{sym}** — {status_text}", expanded=True):
            col1, col2, col3, col4 = st.columns(4)

            with col1:
                st.metric(
                    "Régimen (SMA50>SMA200)",
                    "✅ ON" if regime_on else "❌ OFF",
                    f"{dist_sma200:+.1f}% vs SMA200" if dist_sma200 is not None else "N/A",
                    delta_color="normal" if (dist_sma200 or 0) >= 0 else "inverse",
                )

            with col2:
                if dist_breakout is not None:
                    if dist_breakout >= 0:
                        label = f"✅ +{dist_breakout:.2f}% (roto)"
                        dc = "normal"
                    elif dist_breakout >= -2:
                        label = f"⚡ {dist_breakout:.2f}% (muy cerca)"
                        dc = "normal"
                    elif dist_breakout >= -5:
                        label = f"🟡 {dist_breakout:.2f}% (cerca)"
                        dc = "off"
                    else:
                        label = f"🔴 {dist_breakout:.2f}% (lejos)"
                        dc = "inverse"
                    st.metric(
                        f"Dist. al {donch_label}",
                        label,
                        f"Necesita subir {abs(dist_breakout):.2f}%" if dist_breakout < 0 else "¡Breakout!",
                        delta_color=dc,
                    )
                else:
                    st.metric(f"Dist. al {donch_label}", "N/A")

            with col3:
                label = "Días con régimen ON" if regime_on else "Días con régimen OFF"
                note = ("Sin señal aún" if (regime_on and not entry_signal)
                        else ("¡Señal activa!" if entry_signal else "Bot inactivo"))
                st.metric(label, f"{regime_streak} días", note,
                          delta_color="off" if not entry_signal else "normal")

            with col4:
                if days_since_entry is not None:
                    st.metric(
                        "Última señal de entrada",
                        f"Hace {days_since_entry} días",
                        str(last_entry.date()) if last_entry is not None else "N/A",
                        delta_color="off",
                    )
                else:
                    st.metric("Última señal de entrada", "Nunca (ventana)",
                              "Sin señales en el período")

            # Barra de proximidad al breakout
            if dist_breakout is not None and dist_breakout < 0:
                progress_val = max(0.0, min(1.0, 1.0 - abs(dist_breakout) / 20.0))
                st.markdown("**Proximidad al breakout** (0% = 20% lejos · 100% = en el nivel)")
                st.progress(progress_val, text=f"{dist_breakout:.2f}% del {donch_label}")

            # Info adicional
            info_parts = []
            if days_near > 0:
                info_parts.append(
                    f"⚡ Estuvo cerca del breakout (≤2%) durante **{days_near} días** en los últimos 30"
                )
            if not regime_on and dist_sma200 is not None:
                if dist_sma200 < 0:
                    info_parts.append(
                        f"Para activar el régimen, el precio necesita subir "
                        f"**{abs(dist_sma200):.1f}%** hasta la SMA200"
                    )
            if info_parts:
                st.info("  \n".join(info_parts))

            # Gráfico histórico
            if not hist.empty and len(hist) > 3:
                st.markdown(f"**Histórico distancia al {donch_label} (últimos 60 días)**")
                fig = go.Figure()
                max_y = max(1.0, float(hist["dist_pct"].max()) + 1)
                fig.add_hrect(y0=0, y1=max_y,
                              fillcolor="rgba(0,200,100,0.07)", line_width=0)
                fig.add_hrect(y0=-2, y1=0,
                              fillcolor="rgba(255,200,0,0.10)", line_width=0)
                fig.add_trace(go.Scatter(
                    x=hist["day"], y=hist["dist_pct"],
                    mode="lines+markers",
                    line=dict(color="#4A9EFF", width=2),
                    marker=dict(size=4),
                ))
                fig.add_hline(y=0, line_dash="dash",
                              line_color="rgba(0,200,100,0.7)", line_width=1.5,
                              annotation_text="Breakout", annotation_position="top right")
                fig.add_hline(y=-2, line_dash="dot",
                              line_color="rgba(255,200,0,0.7)", line_width=1,
                              annotation_text="Zona cerca (2%)", annotation_position="bottom right")
                fig.update_layout(
                    height=220,
                    margin=dict(l=10, r=10, t=20, b=10),
                    yaxis_title="% vs Donchian High",
                    xaxis_title=None,
                    showlegend=False,
                    plot_bgcolor="rgba(0,0,0,0)",
                    paper_bgcolor="rgba(0,0,0,0)",
                )
                st.plotly_chart(fig, use_container_width=True)

    # Tabla resumen
    st.markdown("#### Tabla resumen")
    summary_df = diag_df[[
        "symbol", "regime_on", "dist_to_breakout_pct",
        "dist_to_sma200_pct", "regime_streak_days",
        "days_near_breakout_30d", "days_since_last_entry",
    ]].copy()
    summary_df.columns = [
        "Símbolo", "Régimen ON", "Dist. Breakout %", "Dist. SMA200 %",
        "Racha (días)", "Días cerca (30d)", "Días desde última entrada",
    ]
    for col in ["Dist. Breakout %", "Dist. SMA200 %"]:
        summary_df[col] = summary_df[col].apply(
            lambda x: f"{x:+.2f}%" if pd.notna(x) else "N/A"
        )
    st.dataframe(summary_df, use_container_width=True, hide_index=True)

    # Diagnóstico automático
    st.markdown("#### 🧠 Diagnóstico automático")
    for _, row in diag_df.iterrows():
        sym = row["symbol"]
        if row["entry_signal"]:
            st.success(
                f"**{sym}**: Señal de entrada activa hoy. "
                f"Si el bot no operó, revisa `trading_enabled` en settings."
            )
        elif not row["regime_on"]:
            d = row["dist_to_sma200_pct"]
            if d is not None and d > -5:
                st.warning(
                    f"**{sym}**: Régimen OFF pero muy cerca de la SMA200 ({d:+.1f}%). "
                    f"Podría activarse pronto."
                )
            else:
                st.error(
                    f"**{sym}**: Régimen OFF ({d:+.1f}% vs SMA200). "
                    f"Bot inactivo — mercado en tendencia bajista según SMA50/200."
                )
        elif row["dist_to_breakout_pct"] is not None:
            d = row["dist_to_breakout_pct"]
            if d < -10:
                st.warning(
                    f"**{sym}**: Régimen ON pero **{abs(d):.1f}%** lejos del breakout. "
                    f"Mercado lateral o en corrección."
                )
            elif d < 0:
                st.info(
                    f"**{sym}**: Régimen ON y a solo **{abs(d):.1f}%** del breakout. "
                    f"Cerca, pero aún no suficiente."
                )


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
fee_bps = st.sidebar.number_input(
    "Fee total por lado (bps)", min_value=0.0, max_value=50.0, value=10.0, step=1.0
)
slippage_bps = st.sidebar.number_input(
    "Slippage por lado (bps)", min_value=0.0, max_value=100.0, value=5.0, step=1.0
)
st.sidebar.caption("Ejemplo: 10 bps = 0.10% por lado.")

st.sidebar.divider()
auto_refresh = st.sidebar.toggle("Auto-refresh (cada 30s)", value=True)
if auto_refresh:
    st.cache_data.clear()

eur_per_usdc, fx_src = get_usdc_to_eur_rate()

# -----------------------
# Load tables
# -----------------------
equity = read_df(
    "SELECT day, equity_usdc FROM equity_snapshots "
    "WHERE day >= CURRENT_DATE - (%s::int) ORDER BY day",
    params=(days,),
)

trades = read_df(
    "SELECT id, created_at, symbol, side, qty, price, notional, reason FROM trades "
    "WHERE created_at >= NOW() - (%s::int || ' days')::interval ORDER BY created_at ASC",
    params=(days,),
)

positions = read_df(
    "SELECT symbol, qty, avg_price, updated_at FROM positions ORDER BY symbol"
)

# Para diagnóstico: siempre 90 días, con columnas nuevas si existen
# Usamos una query defensiva que no falla si las columnas aún no existen
try:
    signals_diag = read_df(
        """
        SELECT day, symbol, regime_on, entry_signal, exit_signal,
               close, sma200, donchian_high20, donchian_low10, atr14,
               donchian_high_real, donchian_low_real, donch_entry_n, donch_exit_n
        FROM signals
        WHERE day >= CURRENT_DATE - 90
        ORDER BY day ASC, symbol
        """
    )
except Exception:
    # Fallback si las columnas nuevas aún no existen (bot no actualizado aún)
    signals_diag = read_df(
        """
        SELECT day, symbol, regime_on, entry_signal, exit_signal,
               close, sma200, donchian_high20, donchian_low10, atr14
        FROM signals
        WHERE day >= CURRENT_DATE - 90
        ORDER BY day ASC, symbol
        """
    )

signals = read_df(
    """
    SELECT day, symbol, regime_on, entry_signal, exit_signal,
           close, sma200, donchian_high20, donchian_low10, atr14
    FROM signals
    WHERE day >= CURRENT_DATE - (%s::int)
    ORDER BY day DESC, symbol
    """,
    params=(days,),
)

bot_runs = read_df(
    "SELECT id, started_at, finished_at, status, message "
    "FROM bot_runs ORDER BY id DESC LIMIT 50"
)

settings = read_df("SELECT key, value FROM settings ORDER BY key")

# Types fix
if not equity.empty:
    equity["equity_usdc"] = to_float_series(equity["equity_usdc"])

# Filter by symbols
if symbols_sel:
    signals = signals[signals["symbol"].isin(symbols_sel)]
    signals_diag = signals_diag[signals_diag["symbol"].isin(symbols_sel)]
    trades = trades[trades["symbol"].isin(symbols_sel)]
    positions_f = positions[positions["symbol"].isin(symbols_sel)]
else:
    positions_f = positions

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
    eqN = ret = mdd = 0.0

eqN_eur = eqN * eur_per_usdc

c1.metric("Equity (USDC)", f"{eqN:,.2f}")
c2.metric("Equity (EUR aprox)", f"{eqN_eur:,.2f}",
          help=f"1 USDC ≈ {eur_per_usdc:.6f} EUR · {fx_src}" if show_eur else None)
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
p3.metric("Profit Factor",
          "∞" if summary["profit_factor"] == float("inf") else f"{summary['profit_factor']:,.2f}")
p4.metric("Expectancy / trade", f"{summary['expectancy']:,.3f} USDC")
p5.metric("Holding medio", f"{summary['avg_hold_h']:,.1f} h")

st.divider()

# -----------------------
# Equity + Drawdown
# -----------------------
a, b = st.columns([2, 1])

with a:
    st.subheader("Equity curve")
    if equity.empty:
        st.info("Sin datos en equity_snapshots. Ejecuta el bot al menos una vez.")
    else:
        eq_plot = equity.copy()
        metric = st.radio("Unidad", ["USDC", "EUR (aprox)"] if show_eur else ["USDC"], horizontal=True)
        if metric.startswith("EUR"):
            eq_plot["equity"] = eq_plot["equity_usdc"] * eur_per_usdc
            ycol, ytitle = "equity", "equity (EUR aprox)"
        else:
            ycol, ytitle = "equity_usdc", "equity (USDC)"
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
        fig = px.area(dd_df, x="day", y="drawdown",
                      labels={"drawdown": "drawdown", "day": "día"})
        fig.update_layout(height=340, margin=dict(l=10, r=10, t=40, b=10))
        st.plotly_chart(fig, use_container_width=True)

st.divider()

# -----------------------
# DIAGNÓSTICO DE SEÑALES
# -----------------------
diag_df = compute_signal_diagnosis(signals_diag)
render_signal_diagnosis(diag_df)

st.divider()

# -----------------------
# Round-trips
# -----------------------
rt1, rt2 = st.columns([2, 1])

with rt1:
    st.subheader("Round-trips (BUY→SELL) con PnL estimado")
    if roundtrips.empty:
        st.info("Sin round-trips completos en la ventana.")
    else:
        show_cols = [
            "sell_time", "symbol", "qty", "buy_price", "sell_price",
            "buy_notional", "sell_notional", "gross_pnl", "net_pnl",
            "return_pct", "holding_hours",
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
# Positions + Signals
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
            use_container_width=True, hide_index=True,
        )

st.divider()

# -----------------------
# Trades + Settings + Bot runs
# -----------------------
t1, t2 = st.columns([2, 1])

with t1:
    st.subheader("Trades (raw)")
    if trades.empty:
        st.info("Sin trades en la ventana.")
    else:
        st.dataframe(
            trades.sort_values("created_at", ascending=False),
            use_container_width=True, hide_index=True,
        )

with t2:
    st.subheader("Settings")
    st.dataframe(settings, use_container_width=True, hide_index=True)

st.subheader("Bot runs (últimos 50)")

st.dataframe(bot_runs, use_container_width=True, hide_index=True)

st.caption(
    "Notas: PnL estimado (fee+slippage configurables). "
    "Emparejado BUY→SELL por símbolo (asume flat→in→flat)."
)