import os, sqlite3
from .config import ROOT
SCHEMA = """
CREATE TABLE IF NOT EXISTS categories(id INTEGER PRIMARY KEY, parent_id INTEGER, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS funds(id TEXT PRIMARY KEY, name TEXT, amfi_code TEXT, category_id INTEGER, plan TEXT,
  option TEXT, fund_type TEXT, tags TEXT, underlying TEXT, tax_rule TEXT, attributes TEXT);
CREATE TABLE IF NOT EXISTS fund_facts(fund_id TEXT, key TEXT, value TEXT, note TEXT, effective_from TEXT,
  PRIMARY KEY(fund_id,key,effective_from));
CREATE TABLE IF NOT EXISTS tax_rules(id TEXT, effective_from TEXT, data TEXT, PRIMARY KEY(id,effective_from));
CREATE TABLE IF NOT EXISTS stocks(symbol TEXT PRIMARY KEY, name TEXT, isin TEXT, bse_code TEXT);
CREATE TABLE IF NOT EXISTS holdings(fund_id TEXT, as_of TEXT, symbol TEXT, weight TEXT, PRIMARY KEY(fund_id,as_of,symbol));
CREATE TABLE IF NOT EXISTS nav_history(fund_id TEXT, date TEXT, nav TEXT, PRIMARY KEY(fund_id,date));
CREATE TABLE IF NOT EXISTS estimates(id INTEGER PRIMARY KEY AUTOINCREMENT, fund_id TEXT, ts TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, decision TEXT,
  reason TEXT, prev_hash TEXT, hash TEXT);
"""
def conn():
    c = sqlite3.connect(os.environ.get("NAVLAB_DB", str(ROOT / "navlab.db")))
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c
