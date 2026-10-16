# NavLab
Private live mutual-fund NAV estimator (backend-first).

## Run
    pip install -r requirements.txt
    uvicorn app.main:app --reload          # open http://localhost:8000
    NAVLAB_SOURCES=fake uvicorn app.main:app   # demo without internet (fake prices, labelled DEMO)
    pytest

## Build holdings database (recommended)

1. Edit `catalog/funds_catalog.yaml` — set `rupeevest_schemecode` for each fund
   (number in the rupeevest.com fund URL). AMFI codes are already filled where known.
2. Run the batch tool:

       python build_holdings_db.py
       python build_holdings_db.py --only sbi-large-cap-fund   # one fund
       python build_holdings_db.py --dry-run                  # no network

3. Copy YAMLs into the live project and restart:

       cp holdings_db/yaml/*.yaml data/funds/
       uvicorn app.main:app --reload

Single-fund helper (same Rupeevest source):

    python fetch_fund.py 45529 --name "..." --amfi 148945 --out data/funds/foo.yaml

Rupeevest schemecode ≠ AMFI code. Always different systems.

## Before real use (fill in / verify)
1. Set previous official NAV: `POST /api/funds/{id}/nav {"date":"YYYY-MM-DD","nav":"12.3456"}` (or wire daily AMFI file).
2. For FoFs: set `underlying_etf.symbol` in the YAML for the ETF cross-check.
3. Yahoo/NSE adapters are unofficial. Prefer yfinance or a paid source for anything you rely on.

## Extend without code changes
- New fund / new month of holdings: run `fetch_fund.py` (or hand-edit a YAML in `data/funds`) and restart. Weights must sum ~100% and every row needs an NSE symbol.
- New category: `category: [Parent, Child]` in the fund file.
- New tax rule or cost: new dated entry. New source: one class in `app/sources.py` + REGISTRY line + `config/sources.yaml` order.

See **MEMORY.md** for full project status, schema, API, and known gaps.
