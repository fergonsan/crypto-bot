BEGIN;

-- ============================
-- DROP EXISTING TABLES
-- ============================

DROP TABLE IF EXISTS equity_snapshots CASCADE;
DROP TABLE IF EXISTS positions CASCADE;
DROP TABLE IF EXISTS trades CASCADE;
DROP TABLE IF EXISTS signals CASCADE;
DROP TABLE IF EXISTS bot_runs CASCADE;
DROP TABLE IF EXISTS settings CASCADE;

-- ============================
-- SETTINGS
-- ============================

CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

INSERT INTO settings(key, value) VALUES
  ('trading_enabled', 'false'),
  ('max_order_notional_usdc', '300'),
  ('max_asset_exposure_pct', '0.50'),
  ('max_orders_per_day', '2');


-- ============================
-- BOT RUNS
-- ============================

CREATE TABLE bot_runs (
  id SERIAL PRIMARY KEY,
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at TIMESTAMPTZ,
  status TEXT NOT NULL,
  message TEXT
);


-- ============================
-- SIGNALS
-- ============================

CREATE TABLE signals (
  day DATE NOT NULL,
  symbol TEXT NOT NULL,
  regime_on BOOLEAN,
  entry_signal BOOLEAN,
  exit_signal BOOLEAN,
  close NUMERIC,
  sma200 NUMERIC,
  donchian_high20 NUMERIC,
  donchian_low10 NUMERIC,
  atr14 NUMERIC,
  PRIMARY KEY(day, symbol)
);


-- ============================
-- TRADES
-- ============================

CREATE TABLE trades (
  id SERIAL PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  symbol TEXT NOT NULL,
  side TEXT NOT NULL CHECK (side IN ('buy','sell')),
  qty NUMERIC NOT NULL,
  price NUMERIC NOT NULL,
  notional NUMERIC NOT NULL,
  reason TEXT
);

CREATE INDEX idx_trades_created_at ON trades(created_at);
CREATE INDEX idx_trades_symbol ON trades(symbol);


-- ============================
-- POSITIONS (solo del bot)
-- ============================

CREATE TABLE positions (
  symbol TEXT PRIMARY KEY,
  qty NUMERIC NOT NULL DEFAULT 0,
  avg_price NUMERIC,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ============================
-- EQUITY SNAPSHOTS
-- ============================

CREATE TABLE equity_snapshots (
  day DATE PRIMARY KEY,
  equity_usdc NUMERIC NOT NULL
);

ALTER TABLE positions
  ADD COLUMN IF NOT EXISTS entry_time timestamptz,
  ADD COLUMN IF NOT EXISTS peak_close double precision DEFAULT 0,
  ADD COLUMN IF NOT EXISTS hard_stop double precision DEFAULT 0,
  ADD COLUMN IF NOT EXISTS trail_stop double precision DEFAULT 0;

COMMIT;