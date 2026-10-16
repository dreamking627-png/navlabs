"""One import pipeline: YAML file -> validation -> database. New fund / new month = one file."""
import json
from decimal import Decimal as D
from pathlib import Path
from .config import load_yaml, ROOT

class ImportFailed(ValueError):
    pass

def _category(c, path):
    parent = None
    for name in path:
        r = c.execute("SELECT id FROM categories WHERE name=? AND parent_id IS ?", (name, parent)).fetchone()
        parent = r["id"] if r else c.execute("INSERT INTO categories(parent_id,name) VALUES(?,?)", (parent, name)).lastrowid
    return parent

def validate_holdings(items, lo=D("95"), hi=D("105")):
    """
    Strict on symbols (every row must have one) and positive weights.
    Weight sum is allowed a wider band because pure-equity slices from
    Rupeevest often sit slightly outside 98–100.5 after cash is stripped.
    Prefer renormalising in fetch_fund.py so the sum is ~100.
    """
    seen, total = set(), D(0)
    missing_sym = []
    for it in items:
        sym = (it.get("symbol") or "").strip().upper()
        if not sym:
            missing_sym.append(it.get("name") or str(it))
            continue
        if sym in seen:
            raise ImportFailed(f"duplicate symbol {sym}")
        seen.add(sym)
        w = D(str(it["weight"]))
        if w <= 0:
            raise ImportFailed(f"bad weight for {sym}: {w}")
        total += w
    if missing_sym:
        raise ImportFailed(
            f"{len(missing_sym)} holding(s) without symbol: "
            + ", ".join(missing_sym[:8])
            + ("..." if len(missing_sym) > 8 else "")
        )
    if not items:
        raise ImportFailed("no holdings")
    if not (lo <= total <= hi):
        raise ImportFailed(f"weights add up to {total}, expected ~100 (allowed {lo}–{hi})")
    return total

def import_tax_rules(c, path="data/tax/tax_rules.yaml"):
    for r in load_yaml(path):
        c.execute(
            "INSERT OR REPLACE INTO tax_rules VALUES(?,?,?)",
            (r["id"], str(r["effective_from"]), json.dumps(r, default=str)),
        )
    c.commit()

def import_fund(c, path):
    path = Path(path)
    f = load_yaml(path)
    h = f.get("holdings") or {}
    items = h.get("items") or []
    if not items:
        raise ImportFailed(f"{path.name}: holdings.items is empty")
    if not h.get("as_of"):
        raise ImportFailed(f"{path.name}: holdings.as_of is required")

    total = validate_holdings(items)
    cat = _category(c, f["category"])
    c.execute(
        "INSERT OR REPLACE INTO funds VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            f["id"],
            f["name"],
            f.get("amfi_code"),
            cat,
            f.get("plan"),
            f.get("option"),
            f.get("fund_type"),
            json.dumps(f.get("tags", [])),
            json.dumps(f.get("underlying_etf")),
            f.get("tax_rule"),
            json.dumps(f.get("attributes") or {}),
        ),
    )
    for k in f.get("costs") or []:
        c.execute(
            "INSERT OR REPLACE INTO fund_facts VALUES(?,?,?,?,?)",
            (f["id"], k["key"], json.dumps(k["value"]), k.get("note"), str(k["effective_from"])),
        )
    for it in items:
        sym = it["symbol"].strip().upper()
        c.execute(
            "INSERT OR IGNORE INTO stocks(symbol,name,isin,bse_code) VALUES(?,?,?,?)",
            (sym, it["name"], it.get("isin"), it.get("bse_code")),
        )
        c.execute(
            "INSERT OR REPLACE INTO holdings VALUES(?,?,?,?)",
            (f["id"], str(h["as_of"]), sym, str(it["weight"])),
        )
    pn = f.get("previous_nav")
    if pn:
        c.execute(
            "INSERT OR REPLACE INTO nav_history VALUES(?,?,?)",
            (f["id"], str(pn["date"]), str(pn["nav"])),
        )
    c.commit()
    return f["id"], total

def import_all(c):
    """Import every data/funds/*.yaml. Skips files that fail validation and reports them."""
    import_tax_rules(c)
    ok, failed = [], []
    for p in sorted((ROOT / "data/funds").glob("*.yaml")):
        try:
            fid, total = import_fund(c, p)
            ok.append((fid, total))
        except ImportFailed as e:
            failed.append((p.name, str(e)))
        except Exception as e:
            failed.append((p.name, f"{type(e).__name__}: {e}"))
    if failed:
        # Surface problems without aborting the whole startup
        for name, err in failed:
            print(f"[importer] SKIP {name}: {err}")
    return [fid for fid, _ in ok]
