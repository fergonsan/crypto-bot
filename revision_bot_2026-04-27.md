# Revisión Diaria del Crypto-Bot — 2026-04-27

## 🌡️ Condiciones de Mercado

**Bitcoin (BTC):** ~$79,000, acercándose a la resistencia psicológica de $80,000. Tendencia alcista sostenida por flujos de ETFs spot e inversión institucional. Soporte clave en $76,000, resistencia en $80,000-$83,000. Analistas técnicos detectan divergencia bajista en indicadores de momentum — posible consolidación si no rompe $80K de forma decisiva.

**Ethereum (ETH):** ~$2,340, lateralizando en rango $2,100-$2,400 las últimas 6 semanas. Caída significativa desde máximos de $3,769-$3,800 de finales de 2025. Soporte en $2,106-$2,176, con sesgo técnico bajista a corto plazo. ETH claramente underperformando vs BTC.

**Sentimiento:** Fear & Greed Index en zona de miedo (31-46 según fuente), recuperándose desde mínimos extremos. El índice pasó 59 días consecutivos por debajo de 15 (miedo extremo) — racha récord. Ahora en recuperación gradual.

**Dominancia BTC:** 57-60%, cerca de máximos multi-anuales (pico de 65% en junio 2025). El mercado favorece a BTC como activo refugio dentro del cripto. Los ETFs mantienen la dominancia alta ($56.9B en flujos desde enero 2024).

**Volatilidad:** Condiciones mixtas. Liquidaciones bajaron desde los $595M/día de principios de abril a niveles más calmados. Contexto cautelosamente constructivo apoyado por optimismo regulatorio.

**Implicaciones para la estrategia:** El régimen SMA50>SMA200 probablemente está activo para BTC (tendencia alcista consolidada) pero podría ser ambiguo para ETH (caída prolongada). La lateralización de ETH es desfavorable para una estrategia de seguimiento de tendencia. La volatilidad moderada-alta favorece los multiplicadores de ATR actuales.

---

## 📊 Rendimiento Reciente del Bot (Backtest)

### Backtest completo (2021-01-01 a 2026-02-24)
| Métrica | Valor |
|---------|-------|
| Retorno total | +47.10% (~$1,000 → $1,471) |
| Max drawdown | 12.81% |
| Roundtrips totales | 38 |
| Win rate | 55.3% (21W / 17L) |
| PnL total | $460.90 |
| Avg win | $34.52 |
| Avg loss | -$15.54 |
| Profit factor (avg) | 2.22x |

### Por período
- **2023:** 2 roundtrips, 100% win rate, PnL neto +$79.23 — excelente pero muestra baja
- **2024:** 3 roundtrips, 33.3% win rate, PnL neto +$178.92 — un gran trade de BTC ($198 neto) compensó dos pequeñas pérdidas
- **2025 (últimos trades):** ETH dominó la actividad. Último trade cerrado en sept 2025 (ETH stop a $4,308). Desde entonces el bot parece estar sin posiciones abiertas.

### Observaciones clave
1. **El bot no ha operado desde septiembre 2025** — 7 meses sin trades. Esto es consistente con una estrategia de tendencia si el régimen estuvo off o no hubo breakouts, pero merece revisión.
2. **La concentración del PnL en pocos trades grandes** es típica de estrategias trend-following, pero 38 roundtrips en 5 años es muy poco. El bot es muy selectivo.
3. **ETH underperformance actual** podría significar que el par no genera señal de entrada en mucho tiempo dado el rango-bound actual.

---

## 🔧 Sugerencias de Mejora

### Prioridad Alta

**1. `notifier.py` — Sin manejo de errores en envío de Telegram**
- **Qué:** `telegram_send()` no captura excepciones de red. Si Telegram falla (timeout, DNS, rate limit), la excepción se propaga y podría interrumpir el ciclo del bot.
- **Fichero:** `bot/notifier.py`, línea 40
- **Cambio propuesto:**
```python
try:
    urllib.request.urlopen(req, timeout=10)
except Exception as e:
    # Log pero no interrumpir el bot
    print(f"[WARN] telegram_send failed: {e}")
```
- **Por qué:** Un fallo de Telegram no debería detener la ejecución de trades. Esto es un bug latente que puede causar pérdidas si se cae Telegram durante una señal de stop.

**2. `main.py` — `datetime.utcnow()` está deprecated**
- **Qué:** `dt.datetime.utcnow()` está deprecated desde Python 3.12. Debería usar `dt.datetime.now(dt.timezone.utc)`.
- **Fichero:** `bot/main.py`, líneas 158 y 352
- **Cambio:** Reemplazar `dt.datetime.utcnow()` por `dt.datetime.now(dt.timezone.utc)` en ambas ocurrencias.
- **Por qué:** Evitar warnings futuros y mantener compatibilidad con Python 3.12+. También aparece en `intraday_stops.py` línea 144.

**3. `main.py` — Race condition entre daily y intraday stops**
- **Qué:** El daily cycle y el intraday stops usan advisory locks distintos (`bot_daily` y `bot_intraday_stops`), pero ambos actualizan la misma fila en `positions`. Si el intraday cierra una posición mientras el daily está procesando ese símbolo, se puede vender dos veces.
- **Fichero:** `bot/main.py` y `bot/intraday_stops.py`
- **Cambio propuesto:** Antes de ejecutar una venta en `main.py`, volver a leer `positions.qty` y verificar que sigue siendo > 0. Agregar un `SELECT ... FOR UPDATE` en la lectura de posiciones para serializar las escrituras.
- **Por qué:** En producción LIVE, esto podría intentar vender una posición ya cerrada, causando un error de la API de Binance o un saldo negativo.

### Prioridad Media

**4. Considerar reducir riesgo por trade de 2% a 1-1.5% dado el contexto de mercado**
- **Qué:** Con el Fear & Greed Index en zona de miedo (31-46) y ETH en tendencia bajista, un 2% por trade puede ser agresivo. La lateralización genera whipsaws que consumen capital.
- **Fichero:** Variable de entorno `RISK_PER_TRADE` (default en `bot/main.py` línea 38)
- **Cambio:** Considerar `RISK_PER_TRADE=0.015` temporalmente hasta que el mercado defina dirección.
- **Por qué:** El backtest 2024 muestra que 2 de 3 roundtrips fueron perdedores (aunque el net fue positivo por un gran ganador). Reducir riesgo protege contra rachas de whipsaws en lateralización.

**5. `risk.py` — Añadir cap máximo de cantidad en USDC**
- **Qué:** `position_size_usdc()` no tiene un límite intrínseco. Depende de que `main.py` aplique `max_order_notional` después. Sería más robusto que el cálculo de riesgo mismo tenga un cap.
- **Fichero:** `bot/risk.py`
- **Cambio propuesto:** Añadir parámetro opcional `max_notional_usdc` con default None:
```python
def position_size_usdc(..., max_notional_usdc: float | None = None) -> float:
    qty = max(0.0, risk_usdc / stop_distance)
    if max_notional_usdc and entry_price > 0:
        qty = min(qty, max_notional_usdc / entry_price)
    return float(qty)
```
- **Por qué:** Defensa en profundidad. Si se introduce un bug en `main.py` que omita el cap, `risk.py` lo aplicaría igualmente.

**6. ETH puede no ser óptimo para la estrategia actual — considerar SOL/USDC**
- **Qué:** ETH lleva 6 semanas lateralizando ($2,100-$2,400) y ha caído ~38% desde máximos. La dominancia de BTC al 57-60% indica que el capital fluye hacia BTC, no altcoins. SOL ha mostrado mejor momentum relativo en ciclos recientes.
- **Fichero:** Variable de entorno `SYMBOLS` en `bot/main.py`
- **Cambio:** Evaluar mediante backtest la adición de SOL/USDC como tercer par, o como sustituto temporal de ETH.
- **Por qué:** Una estrategia trend-following necesita activos con tendencias limpias. ETH en rango-bound es capital estancado.

### Prioridad Baja

**7. `db.py` — Connection pooling**
- **Qué:** Cada llamada a `get_conn()` crea una conexión nueva. En Railway con PostgreSQL, esto no es un problema grave, pero si se escala (más símbolos, más frecuencia intraday), podría serlo.
- **Fichero:** `bot/db.py`
- **Cambio:** Considerar `psycopg.pool.ConnectionPool` para el futuro.
- **Por qué:** Mejora de eficiencia a largo plazo, no urgente con 2 símbolos y frecuencia actual.

**8. Backtest desactualizado — último dato de febrero 2026**
- **Qué:** La equity curve del backtest más reciente termina el 2026-02-24, hace 2 meses. No refleja las condiciones actuales.
- **Fichero:** `backtesting/backtest.py` (ejecutar con datos actualizados)
- **Cambio:** Re-ejecutar el backtest con datos hasta abril 2026 para validar que los parámetros siguen funcionando.
- **Por qué:** 7 meses sin trades reales + backtest desactualizado = poca visibilidad sobre el comportamiento actual del bot.

---

## ✅ Estado General

**Valoración: El bot está bien diseñado pero necesita atención en tres áreas.**

El código es sólido, bien documentado y con buenas prácticas (advisory locks, dry run, allowlist de símbolos). La estrategia V3 tiene un profit factor de 2.22x en backtest completo, lo cual es bueno para trend-following.

**Alertas:**

1. **Bug crítico latente:** `telegram_send` sin try/catch puede interrumpir ejecución de trades. Corregir ya.
2. **Race condition daily/intraday:** Riesgo de doble venta en producción LIVE. Añadir verificación pre-venta.
3. **7 meses sin actividad:** El bot no ha generado trades desde septiembre 2025. Verificar que está ejecutándose correctamente y que las señales se están evaluando. Re-ejecutar backtest actualizado.

**El mercado actual** (BTC near $80K, ETH lateralizando, sentimiento de miedo) es mixto para esta estrategia. BTC podría dar señal de entrada si rompe $80K con régimen activado, pero ETH probablemente no genere señales de calidad en el corto plazo. Considerar diversificar pares.
