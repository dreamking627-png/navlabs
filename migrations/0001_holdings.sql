CREATE TABLE IF NOT EXISTS fund_portfolios (
  slug TEXT PRIMARY KEY,
  schemecode TEXT NOT NULL,
  as_of TEXT,
  holdings TEXT NOT NULL,
  cash TEXT,
  missing TEXT,
  raw_equity_sum TEXT,
  previous_nav TEXT,
  previous_nav_date TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
