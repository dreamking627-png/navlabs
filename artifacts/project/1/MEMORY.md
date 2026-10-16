# NavLab — Project Memory / Overview

**Last updated from snapshot:** 2026-10-16  
**Project type:** Private live mutual-fund NAV *estimator* (backend-first FastAPI app + minimal SPA frontend)  
**Status:** Working prototype. Holdings fetch pipeline fixed to emit NavLab YAML. Importer skips bad funds instead of crashing. Code-match clarification: Rupeevest schemecode ≠ AMFI code. Core calc + demo mode solid; live price sources still unofficial.

---

## 1. What this project does

NavLab estimates the **current / intra-day NAV** of Indian mutual-fund schemes by:

1. Loading fund metadata + latest disclosed holdings (weights) from YAML files.
2. Fetching live LTP + previous close for every holding symbol from one or more price sources (with fallback + circuit breaker).
3. Computing a weighted portfolio return and applying it to the last known official NAV.
4. Running physics / sanity checks so it refuses to produce a number when data is missing or extreme.
5. Cross-checking against the underlying ETF (when configured) for FoF-style funds.
6. Logging every accept/reject decision in a hash-chained audit log.

It is **not** a full portfolio tracker, not an AMC official NAV publisher, and not production-ready for real money decisions without further hardening.

Primary target fund so far: **Groww Nifty India Defence ETF FoF Direct Growth**.

---

## 2. High-level architecture

```
data/funds/*.yaml  ──► importer.py ──► SQLite (navlab.db)
data/tax/*.yaml    ──┘

config/sources.yaml + REGISTRY ──► sources.Router ──► live quotes

engine.compute()  ← holdings + quotes + prev NAV
       ↓
FastAPI endpoints  ──► static/index.html (SPA)
       ↓
audit_log (hash chain)
```

- **Backend:** FastAPI (`app/main.py`)
- **DB:** SQLite, schema created on every `conn()` call (`app/db.py`)
- **Frontend:** Single-file dark SPA (`static/index.html`) served at `/`
- **Price sources:** Pluggable adapters (`app/sources.py`) — yfinance, unofficial Yahoo, unofficial NSE, and a deterministic `fake` source for offline/demo
- **Calculation:** Pure Decimal arithmetic, never floats (`app/engine.py`)

---

## 3. Directory layout (current)

```
1/                          # project root (extracted from 1.rar)
├── MEMORY.md               # ← this file
├── README.md               # short run instructions
├── requirements.txt        # fastapi, uvicorn, httpx, pyyaml, pytest
├── navlab.db               # live SQLite DB (has Groww fund + some estimates/audit rows)
├── a.txt                   # raw AMFI-style NAV dump (~10.8k lines, dated ~13-Oct-2026)
├── amfi_refine.py          # offline parser: a.txt / similar → refined/schemes.{csv,json} + report
├── fetch_fund.py           # Rupeevest → NavLab YAML (as_of, symbols, renorm, missing report)
├── app/
│   ├── __init__.py
│   ├── main.py             # FastAPI app + all HTTP routes
│   ├── config.py           # ROOT path + YAML loader
│   ├── db.py               # schema + connection helper
│   ├── importer.py         # YAML → validated DB rows
│   ├── engine.py           # NAV estimation math + validation
│   ├── sources.py          # price adapters + Router + market_open helper
│   ├── audit.py            # hash-chained decision log
│   └── check.py            # CLI diagnostic: python -m app.check HAL BEL
├── config/
│   └── sources.yaml        # source order, TTLs, breaker, sanity limits
├── data/
│   ├── funds/
│   │   ├── groww_defence_fof.yaml          # complete, imports cleanly (19 holdings, weights ≈ 99.96)
│   │   └── jioblackrock-flexi-cap-fund-direct-growth.yaml  # BROKEN for import (many symbol: null)
│   └── tax/
│       └── tax_rules.yaml  # equity_oriented rule (STCG 20%, LTCG 12.5% after 1y, exemption)
├── refined/                # output of amfi_refine.py
│   ├── schemes.csv / .json # ~8k+ schemes parsed from a.txt
│   └── report.txt
├── static/
│   └── index.html          # SPA: category tree → fund detail → Estimate button
└── tests/
    └── test_core.py        # unit tests for formula, missing-data refusal, import validation, fallback/breaker, audit
```

---

## 4. Database schema (SQLite)

Created automatically:

| Table          | Purpose |
|----------------|---------|
| `categories`   | Hierarchical (parent_id). Currently Equity → Sectoral / Thematic |
| `funds`        | id (slug), name, amfi_code, category_id, plan, option, fund_type, tags, underlying (ETF JSON), tax_rule, attributes |
| `fund_facts`   | Versioned costs (expense_ratio, exit_load, stamp_duty…) keyed by effective_from |
| `tax_rules`    | Versioned tax JSON by id + effective_from |
| `stocks`       | symbol PK, name, isin, bse_code |
| `holdings`     | fund_id + as_of + symbol + weight (latest as_of used at runtime) |
| `nav_history`  | fund_id + date + nav (official) |
| `estimates`    | id, fund_id, ts, full result JSON |
| `audit_log`    | id, ts, symbol, decision, reason, prev_hash, hash (SHA-256 chain from GENESIS) |

Current DB contents (from the bundled `navlab.db`):
- 1 fund: `groww-nifty-india-defence-etf-fof-dg` (19 holdings as of 2026-10-11)
- No rows in `nav_history` yet (previous_nav was null in YAML)
- 5 previous estimates, 62 audit rows
- Categories: Equity → Sectoral / Thematic

---

## 5. API surface

| Method | Path | Notes |
|--------|------|-------|
| GET | `/` | Serves `static/index.html` |
| GET | `/api/categories` | Tree of categories + nested funds |
| GET | `/api/funds/{fid}` | Fund detail + costs + tax + last_nav |
| GET | `/api/funds/{fid}/holdings` | Latest holdings snapshot |
| POST | `/api/funds/{fid}/nav` | Body `{date, nav}` — store official NAV |
| POST | `/api/funds/{fid}/estimate` | Live estimate (uses Router + engine) |
| GET | `/api/estimates/{eid}` | Replay a stored estimate |
| GET | `/api/health` | sources list, open circuits, audit chain validity, fund count |

Startup event always runs `importer.import_all()` so YAML changes are picked up on restart.

---

## 6. Estimation flow (POST /estimate)

1. Load latest holdings for the fund.
2. Collect symbols (+ underlying ETF symbol if present).
3. `router.get_quotes(symbols)` walks the ordered source list; uses cache, respects circuit breaker, drops stale quotes during market hours.
4. Each quote is validated (`engine.validate_quote`): positive prices + absolute change ≤ `max_abs_change_pct` (default 20%). Rejects go into audit_log.
5. `engine.compute(holdings, good_quotes, prev_nav, sanity_pct)`:
   - Weighted sum of individual % changes.
   - Status = `OK` | `INCOMPLETE` (any missing symbol) | `REJECTED` (portfolio change > sanity limit, default 10%).
   - Never invents a number when data is incomplete.
6. Optional ETF cross-check: if gap > 1% vs underlying ETF move → warning.
7. Confidence: `DEMO` (fake source), `HIGH` (single real source), `DEGRADED` (mixed), `NONE` (bad status).
8. Full result JSON stored in `estimates` table; audit entry written.

Market hours helper: IST Mon–Fri 09:15–15:30.

---

## 7. Price sources (current)

Configured order in `config/sources.yaml`: `[yfinance, yahoo, nse]`

| Name | Status | Notes |
|------|--------|-------|
| `yfinance` | Library-based | Uses `.NS` suffix. No real quote timestamp → staleness check is weak. |
| `yahoo` | Unofficial chart endpoint | `query1.finance.yahoo.com` — unverified, may break. |
| `nse` | Unofficial NSE API | Needs cookie from homepage first — unverified / fragile. |
| `fake` | Deterministic | For tests & `NAVLAB_SOURCES=fake`. Always available offline. |

Circuit breaker: 3 failures → 300 s cooldown. Cache TTL 5 s. Max quote age 300 s during market hours.

**Known risk:** Yahoo & NSE adapters are marked UNVERIFIED in code comments and have never been hardened against live endpoint changes / blocking.

---

## 8. Current fund data status

### Groww Nifty India Defence ETF FoF Direct Growth
- **id:** `groww-nifty-india-defence-etf-fof-dg`
- **YAML:** complete, all 19 symbols filled, weights sum to ~99.96 (passes validation)
- **amfi_code:** `152925` (6-digit AMFI scheme code). ISIN `INF666M01IQ3` stored in attributes.
- **underlying_etf.symbol:** still `null` → ETF cross-check disabled
- **previous_nav:** null → estimates only return change %, not absolute NAV until you POST one
- **costs / tax:** present (expense 0.14%, exit load 1% <30d, equity_oriented tax rule)
- **In DB:** yes

### SBI Nifty Next 50 Index Fund Direct Growth (newly fetched)
- **id:** `sbi-nifty-next-50-index-fund-direct-growth`
- **Source:** `fetch_fund.py` from Rupeevest schemecode `45529`
- **amfi_code:** `148945`
- **holdings:** 50 symbols, as_of 2026-09-30, weights renormalised to 100%
- **In data/funds/:** yes (imports cleanly)

### JioBlackRock Flexi Cap Fund Direct Growth
- **id:** `jioblackrock-flexi-cap-fund-direct-growth`
- **YAML:** present but **many holdings have `symbol: null`**
- **Importer skips it** at startup with a clear message (does not crash the app)
- previous_nav is set (2026-10-09 → 9.51)
- **Not in current DB** for the above reason

---

## 9. Supporting scripts

| Script | Purpose |
|--------|---------|
| `amfi_refine.py` | Parse raw AMFI / portal NAV text (like `a.txt`) → clean schemes.csv/json + quality report. Handles multiple formats, normalises plan/option, detects stale NAVs. Already run once → `refined/` (~8296 schemes). |
| `fetch_fund.py` | **Rewritten 2026-10-16.** Fetches equity holdings from Rupeevest and writes a **full NavLab-compatible YAML** (id, name, category, holdings.as_of/items, tax_rule, attributes…). Reports missing symbols, extracts disclosure date (`invdate`), renormalises weights to 100%, uses PyYAML (safe quoting). See usage below. |
| `python -m app.check HAL BEL` | Quick live test of Yahoo + NSE adapters for given symbols. |

### fetch_fund.py usage
```bash
python fetch_fund.py 45529 \
  --name "SBI Nifty Next 50 Index Fund Direct Growth" \
  --amfi 148945 \
  --category "Equity,Index" \
  --plan Direct --option Growth --type Index \
  --out data/funds/sbi-nifty-next-50-dg.yaml
```
**Important:** Rupeevest `schemecode` ≠ AMFI scheme code. Always look up the 6-digit AMFI code in `refined/schemes.csv` and pass `--amfi`.

---

## 10. How to run (from project root)

```bash
pip install -r requirements.txt
# optional: pip install yfinance   # only if using the yfinance source

uvicorn app.main:app --reload          # http://localhost:8000
NAVLAB_SOURCES=fake uvicorn app.main:app   # offline demo mode
pytest
```

Env vars:
- `NAVLAB_DB` — override DB path (tests use a temp file)
- `NAVLAB_SOURCES` — comma list overriding `config/sources.yaml` order

---

## 11. Known issues / gaps / “little errors”

1. **JioBlackRock unimportable** — many `symbol: null`. Importer now **skips** it with a log line instead of crashing startup. Refetch via `fetch_fund.py` (if you have the Rupeevest code) or fill symbols manually.
2. **No previous official NAV for Groww / SBI** — estimates only give % change until you POST `/api/funds/.../nav` or wire the daily AMFI file.
3. **Underlying ETF symbol missing** on Groww FoF — cross-check inactive.
4. **amfi_code present but unused for auto-NAV** — no automatic official NAV pull from mfapi.in / AMFI daily file yet. Daily NAV from `refined/schemes.csv` (or a fresh AMFI download each morning) is the right source; it is published late evening so use previous business day.
5. **Rupeevest ≠ AMFI codes** — confirmed. Rupeevest uses its own schemecode (e.g. 45529); AMFI uses 6-digit codes (e.g. 148945). Always map via `refined/schemes.csv` (by name/ISIN) when setting `--amfi`.
6. **Yahoo / NSE sources unverified** — may break or be rate-limited; yfinance is the most reliable of the three but still unofficial.
7. **yfinance has no real timestamp** — staleness filter is weak for that source.
8. **Frontend is minimal** — no auth, no history charts.
9. **Tax rules & costs are decorative** — stored/returned by API but not used in the estimate math.
10. **Importer weight band is 95–105%** (was 98–100.5). `fetch_fund.py` renormalises equity to 100% by default so imports succeed. Cash/debt is reported but not stored as holdings.
11. **Only equity holdings are fetched** — hybrid/debt funds and pure FoFs that only list the underlying ETF will not work well. Prefer equity & index funds.
12. **a.txt / refined data** — ~8.3k schemes ready for lookup / daily NAV, not yet auto-wired into the live app.
13. **Dates in sample data are in 2026** — treat as sample / test environment data.

---

## 12. Design principles already present (worth preserving)

- **Refuse to guess** — missing prices → status `INCOMPLETE`, no number.
- **Decimal everywhere** in the engine.
- **Hash-chained audit log** — tamper-evident decision history.
- **Versioned facts** (costs, tax) via `effective_from`.
- **New fund / new month = one YAML file** — no code change required for ordinary extension.
- **Source adapters are pure** — easy to add another by subclassing `Source` + one REGISTRY line.
- **Circuit breaker + ordered fallbacks** — resilience without silent failure.
- **Demo mode labelled** — confidence becomes `DEMO` when fake source is used.

---

## 13. Suggested next work (not done yet)

Natural follow-ups — **not** implemented yet:

1. **Daily NAV ingest** — read previous business day NAV from `refined/schemes.csv` (or a fresh AMFI download) into `nav_history` using `amfi_code`. This unblocks absolute NAV estimates.
2. **Batch-fetch 10–20 equity/index funds** with `fetch_fund.py` (once or twice a month). Always pass `--amfi` from schemes.csv.
3. **Accuracy loop** — each evening compare estimate vs official NAV for the selected funds; that is the real proof.
4. Set `underlying_etf.symbol` for Groww FoF and verify the cross-check.
5. Harden or drop Yahoo/NSE adapters; prefer yfinance + optional paid source.
6. Optional: UI to set previous NAV / pick funds from the refined catalogue.

---

## 14. Quick mental model

> “YAML describes the fund. On every estimate request we pull live prices for its holdings, weight the moves, apply them to the last official NAV, and refuse to speak if anything is missing or looks impossible. Everything is logged. The UI is a thin shell over the API.”

Reading this file should be enough to understand the current state of the site without opening any other source file.
