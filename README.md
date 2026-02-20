# Crypto Bot (Binance Spot)

Bot de trading automatizado para Binance que opera en modo spot usando estrategias técnicas.

## 📁 Estructura del Proyecto

- **bot/**: Código principal del bot (ejecuta diariamente mediante cron)
- **dashboard/**: Dashboard web con Streamlit para visualizar resultados
- **sql/**: Esquema de base de datos PostgreSQL

**Deploy target**: Railway (Postgres + bot-runner cron + dashboard web)

## 🎯 Archivos Principales y Dónde Modificar

### ⚠️ **strategy.py** - MODIFICA AQUÍ LA ESTRATEGIA

Este es el archivo más importante para cambiar la lógica de trading.

**Funciones clave:**
- `compute_indicators()`: Calcula indicadores técnicos (SMA200, Donchian, ATR)
  - **Modifica aquí** para agregar nuevos indicadores
- `decide()`: Decide señales de entrada/salida
  - **Modifica aquí** para cambiar cuándo comprar/vender
  - Línea ~63: Condición de régimen (cuándo el bot opera)
  - Línea ~66: Condición de entrada (cuándo comprar)
  - Línea ~67: Condición de salida (cuándo vender)

**Estrategia actual:**
- **Régimen**: Precio > SMA200 (mercado alcista)
- **Entrada**: Régimen ON + precio rompe máximo de 20 períodos (Donchian High)
- **Salida**: Precio cae por debajo del mínimo de 10 períodos (Donchian Low) O régimen se apaga

### **main.py** - Flujo Principal del Bot

Orquesta todo el proceso:
1. Lee configuración desde BD
2. Calcula señales para todos los símbolos
3. Ejecuta órdenes si trading está habilitado
4. Envía notificaciones por Telegram

**Configuración:**
- Variables de entorno: `SYMBOLS`, `TIMEFRAME`, `DRY_RUN`
- Base de datos (tabla `settings`): `trading_enabled`, `max_order_notional_usdc`, etc.

### **risk.py** - Gestión de Riesgo

- `position_size_usdc()`: Calcula tamaño de posición
  - **Modifica aquí** el multiplicador del ATR (actualmente 2.0) o el % de riesgo (1%)

**Lógica actual:**
- Riesgo: 1% del equity por operación
- Stop loss: 2 * ATR por debajo del precio de entrada

### **db.py** - Base de Datos

Funciones auxiliares para PostgreSQL:
- `get_setting()`: Lee configuración
- `create_run()`: Registra ejecuciones del bot

### **binance_client.py** - Conexión a Binance

Crea la conexión con Binance usando CCXT.
Requiere: `BINANCE_API_KEY` y `BINANCE_API_SECRET`

### **notifier.py** - Notificaciones Telegram

Envía mensajes por Telegram (opcional).
Requiere: `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`

## 🔧 Configuración

### Variables de Entorno

```bash
# Binance (requerido)
BINANCE_API_KEY=tu_api_key
BINANCE_API_SECRET=tu_api_secret

# Base de datos (requerido)
DATABASE_URL=postgresql://user:pass@host:port/db

# Trading (opcional)
SYMBOLS=BTC/USDC,ETH/USDC  # Pares a operar
TIMEFRAME=1d                # Timeframe: 1d, 4h, 1h, etc.
DRY_RUN=true                # true = simulación, false = órdenes reales

# Telegram (opcional)
TELEGRAM_BOT_TOKEN=tu_token
TELEGRAM_CHAT_ID=tu_chat_id
```

### Configuración en Base de Datos

Actualiza la tabla `settings`:

```sql
UPDATE settings SET value = 'true' WHERE key = 'trading_enabled';
UPDATE settings SET value = '300' WHERE key = 'max_order_notional_usdc';
UPDATE settings SET value = '0.50' WHERE key = 'max_asset_exposure_pct';
UPDATE settings SET value = '2' WHERE key = 'max_orders_per_day';
```

## 📊 Base de Datos

### Tablas Principales

- **signals**: Señales calculadas cada día (régimen, entrada, salida)
- **trades**: Órdenes ejecutadas (compras y ventas)
- **positions**: Posiciones abiertas del bot
- **equity_snapshots**: Histórico de equity diario
- **bot_runs**: Registro de ejecuciones del bot
- **settings**: Configuración del bot

## 🚀 Cómo Modificar la Estrategia

1. **Agregar indicadores**: Modifica `compute_indicators()` en `strategy.py`
2. **Cambiar lógica de entrada**: Modifica `decide()` línea ~66 en `strategy.py`
3. **Cambiar lógica de salida**: Modifica `decide()` línea ~67 en `strategy.py`
4. **Cambiar filtro de régimen**: Modifica `decide()` línea ~63 en `strategy.py`
5. **Ajustar riesgo**: Modifica `position_size_usdc()` en `risk.py`

## ⚠️ Advertencias

- **Siempre prueba en DRY_RUN=true primero**
- El bot opera con dinero real cuando `DRY_RUN=false` y `trading_enabled=true`
- Revisa los límites de riesgo en la configuración antes de activar trading real
- El bot mantiene su propio registro de posiciones (tabla `positions`), no sincroniza automáticamente con Binance

## 📝 Notas

- El bot se ejecuta periódicamente (típicamente una vez al día)
- Siempre calcula señales, incluso si trading está deshabilitado
- Las señales se guardan en la BD para análisis histórico
- El dashboard permite visualizar el rendimiento y las señales
