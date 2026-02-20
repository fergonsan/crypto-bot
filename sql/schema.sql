CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- Kill switch y límites
INSERT INTO settings(key, value) VALUES
  ('trading_enabled', 'false'),
  ('max_order_notional_usdc', '300'),
  ('max_asset_exposure_pct', '0.50'),
  ('max_orders_per_day', '2')
ON CONFLICT (key) DO NOTHING;

CREATE TABLE IF NOT EXISTS bot_runs (
  id BIGSERIAL PRIMARY KEY,
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at TIMESTAMPTZ,
  status TEXT NOT NULL DEFAULT 'running',
  message TEXT
);

CREATE TABLE IF NOT EXISTS signals (
  day DATE NOT NULL,
  symbol TEXT NOT NULL,
  regime_on BOOLEAN NOT NULL,
  entry_signal BOOLEAN NOT NULL,
  exit_signal BOOLEAN NOT NULL,
  close NUMERIC,
  sma200 NUMERIC,
  donchian_high20 NUMERIC,
  donchian_low10 NUMERIC,
  atr14 NUMERIC,
  PRIMARY KEY(day, symbol)
);

CREATE TABLE IF NOT EXISTS trades (
  id BIGSERIAL PRIMARY KEY,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL CHECK (side IN ('buy','sell')),
  qty NUMERIC NOT NULL,
  price NUMERIC NOT NULL,
  notional NUMERIC NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
  day DATE PRIMARY KEY,
  equity_usdc NUMERIC NOT NULL
);
