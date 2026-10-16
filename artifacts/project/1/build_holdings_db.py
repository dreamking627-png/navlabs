#!/usr/bin/env python3
"""
Build a holdings database (NavLab-ready YAML files) from a fund catalog.

This is the batch tool for step 1 of scaling NavLab:
  catalog → fetch holdings (Rupeevest) → organized YAML database

Usage
-----
  # 1) Create / edit a catalog (see catalog/funds_catalog.yaml)
  # 2) Fill rupeevest_schemecode for each fund you want to fetch
  # 3) Run:

      python build_holdings_db.py
      python build_holdings_db.py --catalog catalog/funds_catalog.yaml --out holdings_db
      python build_holdings_db.py --only sbi-large-cap-fund   # one fund
      python build_holdings_db.py --dry-run                   # resolve AMFI only, no fetch
      python build_holdings_db.py --enrich-amfi               # fill missing amfi_code from schemes.csv

  # 4) Copy successful YAMLs into the live project:

      cp holdings_db/yaml/*.yaml data/funds/
      # restart uvicorn so importer picks them up

Catalog format (YAML list)
--------------------------
  - id: sbi-large-cap-fund          # stable slug (optional; auto from name)
    name: SBI Large Cap Fund
    rupeevest_schemecode: "12345"   # REQUIRED to fetch (not AMFI code)
    amfi_code: "119598"             # optional; enriched from schemes.csv if missing
    category: [Equity, Large Cap]
    plan: Direct
    option: Growth
    fund_type: Active               # Active | Index | ETF | FoF
    tags: [Large Cap, Direct, Growth]
    tax_rule: equity_oriented
    enabled: true                   # set false to skip

Notes
-----
  - Rupeevest schemecode ≠ AMFI scheme code. Always different systems.
  - Only equity rows are stored; cash/debt reported in attributes.
  - Weights renormalised to 100% so NavLab importer accepts them.
  - FoFs that only hold an ETF may return 0–2 equity lines — review those.
  - Unofficial Rupeevest endpoint; use sparingly (monthly), with delay.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from difflib import SequenceMatcher
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
DEFAULT_CATALOG = ROOT / "catalog" / "funds_catalog.yaml"
DEFAULT_OUT = ROOT / "holdings_db"
SCHEMES_CSV = ROOT / "refined" / "schemes.csv"

RV_URL = "https://www.rupeevest.com/home/get_holding_asset?schemecode={code}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}


# ── helpers ──────────────────────────────────────────────────────────────────

def slugify(name: str, fallback: str = "fund") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return (s[:70] or fallback).rstrip("-")


def load_catalog(path: Path) -> list[dict]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit(f"Catalog must be a YAML list: {path}")
    out = []
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            raise SystemExit(f"Catalog row {i} is not a mapping")
        if not row.get("name") and not row.get("rupeevest_schemecode"):
            raise SystemExit(f"Catalog row {i} needs at least name or rupeevest_schemecode")
        row = dict(row)
        row.setdefault("enabled", True)
        row.setdefault("plan", "Direct")
        row.setdefault("option", "Growth")
        row.setdefault("fund_type", "Active")
        row.setdefault("tax_rule", "equity_oriented")
        row.setdefault("category", ["Equity", "Others"])
        row.setdefault("tags", [])
        if not row.get("id"):
            row["id"] = slugify(row.get("name") or f"fund-{row.get('rupeevest_schemecode')}")
        out.append(row)
    return out


def load_schemes(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _norm(s: str) -> str:
    s = re.sub(r"[^A-Z0-9 ]+", " ", (s or "").upper())
    noise = (
        "DIRECT REGULAR GROWTH PLAN OPTION FUND THE AND OF IDCW PAYOUT "
        "REINVEST REINVESTMENT DIVIDEND BONUS SUN LIFE SL"
    ).split()
    for w in noise:
        s = re.sub(rf"\b{w}\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def match_amfi(name: str, schemes: list[dict], plan: str = "Direct", option: str = "Growth") -> tuple[str | None, float, str]:
    """Return (amfi_code, score, matched_name)."""
    if not schemes or not name:
        return None, 0.0, ""
    pool = [
        r for r in schemes
        if (not plan or r.get("plan") == plan) and (not option or r.get("option") == option)
    ] or schemes
    n = _norm(name)
    best, score = None, 0.0
    for r in pool:
        for key in ("name", "base_name"):
            sc = SequenceMatcher(None, n, _norm(r.get(key, ""))).ratio()
            if sc > score:
                score, best = sc, r
    if best and score >= 0.72:
        return best.get("code"), score, best.get("name") or ""
    return None, score, (best.get("name") if best else "") or ""


def fetch_rupeevest(schemecode: str, timeout: int = 30) -> dict:
    url = RV_URL.format(code=schemecode)
    headers = {**HEADERS, "Referer": f"https://www.rupeevest.com/Mutual-Funds-India/{schemecode}"}
    r = requests.get(url, headers=headers, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def build_symbol_lookups(stock_data_search: list) -> tuple[dict, dict]:
    by_name, by_fincode = {}, {}
    for item in stock_data_search or []:
        name = (item.get("compname") or "").strip()
        fin = item.get("fincode")
        parts = [p.strip() for p in (item.get("stock_search") or "").split("|")]
        sym = parts[2] if len(parts) >= 3 and parts[2] else ""
        if not sym:
            continue
        if name:
            by_name[name] = sym
        if fin is not None:
            by_fincode[str(fin)] = sym
    return by_name, by_fincode


def extract_equity(data: dict) -> tuple[list, list, str | None, dict, Decimal]:
    """equity items, missing names, as_of, cash dict, raw equity weight sum."""
    by_name, by_fincode = build_symbol_lookups(data.get("stock_data_search") or [])
    equity, missing = [], []
    as_of = None
    for h in data.get("hold_eq") or []:
        name = (h.get("compname") or "").strip()
        raw_w = h.get("holdpercentage")
        if not name or raw_w is None:
            continue
        try:
            w = Decimal(str(raw_w))
        except Exception:
            continue
        if w <= 0:
            continue
        inv = h.get("invdate")
        if inv and as_of is None:
            as_of = str(inv)[:10]
        sym = by_fincode.get(str(h.get("fincode") or "")) or by_name.get(name) or ""
        if not sym:
            missing.append(name)
            continue
        equity.append({"name": name, "symbol": sym.upper(), "weight": w})
    cash: dict[str, Decimal] = {}
    for h in (data.get("hold_cash") or []) + (data.get("hold_debt") or []) + (data.get("hold_others") or []):
        nm = (h.get("compname") or "").strip() or "other"
        try:
            cash[nm] = cash.get(nm, Decimal(0)) + Decimal(str(h.get("holdpercentage") or 0))
        except Exception:
            pass
    raw_sum = sum((i["weight"] for i in equity), Decimal(0))
    return equity, missing, as_of, cash, raw_sum


def renormalise(items: list, target: Decimal = Decimal("100")) -> list:
    total = sum((i["weight"] for i in items), Decimal(0))
    if total <= 0:
        return items
    factor = target / total
    out = []
    for i in items:
        out.append({
            **i,
            "weight": (i["weight"] * factor).quantize(Decimal("0.01"), ROUND_HALF_UP),
        })
    residual = target - sum((i["weight"] for i in out), Decimal(0))
    if out and residual:
        out[0]["weight"] = (out[0]["weight"] + residual).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return out


def to_navlab_yaml(
    *,
    fund: dict,
    equity: list,
    as_of: str,
    missing: list,
    cash: dict,
    raw_eq_sum: Decimal,
    schemecode: str,
) -> dict:
    return {
        "id": fund["id"],
        "name": fund["name"],
        "amfi_code": str(fund["amfi_code"]) if fund.get("amfi_code") else None,
        "category": fund.get("category") or ["Equity", "Others"],
        "fund_type": fund.get("fund_type") or "Active",
        "plan": fund.get("plan") or "Direct",
        "option": fund.get("option") or "Growth",
        "tags": fund.get("tags") or [fund.get("plan"), fund.get("option"), fund.get("fund_type")],
        "underlying_etf": fund.get("underlying_etf"),
        "attributes": {
            "rupeevest_schemecode": str(schemecode),
            "source": "rupeevest",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "raw_equity_weight_sum": float(raw_eq_sum),
            "cash_and_other": {k: float(v) for k, v in cash.items()},
            "symbols_missing_at_fetch": missing,
            "tickertape_slug": fund.get("tickertape_slug"),
        },
        "previous_nav": fund.get("previous_nav"),  # usually null; filled later from AMFI daily
        "tax_rule": fund.get("tax_rule") or "equity_oriented",
        "costs": fund.get("costs") or [],
        "holdings": {
            "as_of": as_of,
            "items": [
                {"name": i["name"], "symbol": i["symbol"], "weight": float(i["weight"])}
                for i in equity
            ],
        },
    }


def write_yaml(path: Path, doc: dict, header_lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False)
    header = "".join(f"# {line}\n" for line in header_lines)
    path.write_text(header + body, encoding="utf-8")


# ── main pipeline ────────────────────────────────────────────────────────────

def process_fund(
    fund: dict,
    *,
    schemes: list[dict],
    out_yaml_dir: Path,
    dry_run: bool,
    enrich_amfi: bool,
    delay: float,
) -> dict:
    """Returns a result dict for the manifest/report."""
    result = {
        "id": fund["id"],
        "name": fund.get("name"),
        "status": "pending",
        "amfi_code": fund.get("amfi_code"),
        "rupeevest_schemecode": fund.get("rupeevest_schemecode"),
        "holdings_count": 0,
        "missing_symbols": 0,
        "as_of": None,
        "yaml": None,
        "error": None,
        "amfi_match_score": None,
    }

    if not fund.get("enabled", True):
        result["status"] = "skipped_disabled"
        return result

    # Enrich AMFI if missing
    if enrich_amfi or not fund.get("amfi_code"):
        code, score, matched = match_amfi(
            fund.get("name") or "",
            schemes,
            plan=fund.get("plan") or "Direct",
            option=fund.get("option") or "Growth",
        )
        result["amfi_match_score"] = round(score, 3)
        if code and score >= 0.72:
            fund["amfi_code"] = code
            result["amfi_code"] = code
            result["amfi_matched_name"] = matched

    schemecode = str(fund.get("rupeevest_schemecode") or "").strip()
    if not schemecode:
        result["status"] = "needs_rupeevest_code"
        result["error"] = "Set rupeevest_schemecode in the catalog to fetch holdings"
        return result

    if dry_run:
        result["status"] = "dry_run_ok"
        return result

    try:
        data = fetch_rupeevest(schemecode)
        equity, missing, as_of, cash, raw_sum = extract_equity(data)
        if not equity:
            result["status"] = "no_equity_holdings"
            result["error"] = "Rupeevest returned no equity rows (FoF/debt/gold or bad code?)"
            result["missing_symbols"] = len(missing)
            return result

        as_of = as_of or date.today().isoformat()
        equity = renormalise(equity)
        doc = to_navlab_yaml(
            fund=fund,
            equity=equity,
            as_of=as_of,
            missing=missing,
            cash=cash,
            raw_eq_sum=raw_sum,
            schemecode=schemecode,
        )
        ypath = out_yaml_dir / f"{fund['id']}.yaml"
        write_yaml(
            ypath,
            doc,
            [
                f"Generated by build_holdings_db.py",
                f"rupeevest={schemecode}  amfi={fund.get('amfi_code')}  as_of={as_of}",
                f"equity={len(equity)}  missing_symbols={len(missing)}  raw_eq_sum={float(raw_sum):.2f}",
                f"Copy into data/funds/ and restart the app to estimate NAV.",
            ],
        )
        result.update(
            status="ok",
            holdings_count=len(equity),
            missing_symbols=len(missing),
            as_of=as_of,
            yaml=str(ypath.relative_to(out_yaml_dir.parent) if out_yaml_dir.parent in ypath.parents else ypath),
        )
        if missing:
            result["missing_names"] = missing[:15]
        if delay > 0:
            time.sleep(delay)
    except Exception as e:
        result["status"] = "error"
        result["error"] = f"{type(e).__name__}: {e}"
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Build NavLab holdings YAML database from a fund catalog")
    ap.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG, help="Fund catalog YAML")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output directory (yaml/ + reports)")
    ap.add_argument("--schemes", type=Path, default=SCHEMES_CSV, help="refined/schemes.csv for AMFI match")
    ap.add_argument("--only", nargs="*", help="Only process these fund ids")
    ap.add_argument("--dry-run", action="store_true", help="No network fetch; resolve AMFI only")
    ap.add_argument("--enrich-amfi", action="store_true", help="Fill missing amfi_code from schemes.csv")
    ap.add_argument("--delay", type=float, default=1.5, help="Seconds between Rupeevest calls")
    ap.add_argument("--write-catalog", action="store_true", help="Write back enriched amfi_code into catalog")
    args = ap.parse_args()

    if not args.catalog.exists():
        raise SystemExit(
            f"Catalog not found: {args.catalog}\n"
            f"Create one (see catalog/funds_catalog.yaml) or pass --catalog PATH"
        )

    funds = load_catalog(args.catalog)
    if args.only:
        want = set(args.only)
        funds = [f for f in funds if f["id"] in want]
        if not funds:
            raise SystemExit(f"No catalog entries matched --only {args.only}")

    schemes = load_schemes(args.schemes)
    out = args.out
    yaml_dir = out / "yaml"
    yaml_dir.mkdir(parents=True, exist_ok=True)

    print(f"Catalog: {args.catalog}  ({len(funds)} funds)")
    print(f"Output:  {out}")
    print(f"AMFI db: {args.schemes}  ({len(schemes)} rows)")
    print(f"Mode:    {'DRY-RUN' if args.dry_run else 'FETCH'}")
    print("-" * 60)

    results = []
    for fund in funds:
        print(f"→ {fund['id']}  ({fund.get('name')})")
        r = process_fund(
            fund,
            schemes=schemes,
            out_yaml_dir=yaml_dir,
            dry_run=args.dry_run,
            enrich_amfi=args.enrich_amfi or not fund.get("amfi_code"),
            delay=args.delay,
        )
        results.append(r)
        status = r["status"]
        extra = ""
        if status == "ok":
            extra = f"  holdings={r['holdings_count']}  missing={r['missing_symbols']}  as_of={r['as_of']}"
        elif r.get("error"):
            extra = f"  {r['error']}"
        elif status == "needs_rupeevest_code":
            extra = "  (add rupeevest_schemecode to catalog)"
        print(f"  {status}{extra}")

    # Manifest + human report
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "_manifest.json"
    manifest_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    ok = [r for r in results if r["status"] == "ok"]
    need_code = [r for r in results if r["status"] == "needs_rupeevest_code"]
    errors = [r for r in results if r["status"] == "error"]
    other = [r for r in results if r not in ok and r not in need_code and r not in errors]

    lines = [
        f"Holdings DB build  {datetime.now(timezone.utc).isoformat()}",
        f"Catalog: {args.catalog}",
        f"OK: {len(ok)}   need_rupeevest_code: {len(need_code)}   errors: {len(errors)}   other: {len(other)}",
        "",
        "=== OK ===",
    ]
    for r in ok:
        lines.append(
            f"  {r['id']:40s}  amfi={r.get('amfi_code') or '-':8s}  "
            f"n={r['holdings_count']:3d}  missing={r['missing_symbols']}  as_of={r['as_of']}"
        )
    if need_code:
        lines += ["", "=== NEED rupeevest_schemecode (add to catalog, then re-run) ==="]
        for r in need_code:
            lines.append(f"  {r['id']:40s}  {r.get('name')}")
    if errors:
        lines += ["", "=== ERRORS ==="]
        for r in errors:
            lines.append(f"  {r['id']:40s}  {r.get('error')}")
    if other:
        lines += ["", "=== OTHER ==="]
        for r in other:
            lines.append(f"  {r['id']:40s}  {r['status']}  {r.get('error') or ''}")

    lines += [
        "",
        "Next steps:",
        "  1. Fill rupeevest_schemecode for funds listed under NEED",
        "     (open the fund on rupeevest.com — the number in the URL is the schemecode)",
        "  2. Re-run:  python build_holdings_db.py",
        "  3. Copy:    cp holdings_db/yaml/*.yaml data/funds/",
        "  4. Restart the NavLab app so importer loads the new YAMLs",
        "  5. Set previous NAV (POST /api/funds/{id}/nav) or wire daily AMFI file",
    ]
    report_path = out / "_report.txt"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("-" * 60)
    print("\n".join(lines))
    print(f"\nWrote {manifest_path}")
    print(f"Wrote {report_path}")

    # Optionally write enriched amfi back into catalog
    if args.write_catalog and not args.dry_run:
        by_id = {r["id"]: r for r in results}
        raw = yaml.safe_load(args.catalog.read_text(encoding="utf-8"))
        for row in raw:
            rid = row.get("id") or slugify(row.get("name") or "")
            if rid in by_id and by_id[rid].get("amfi_code") and not row.get("amfi_code"):
                row["amfi_code"] = str(by_id[rid]["amfi_code"])
        args.catalog.write_text(
            yaml.safe_dump(raw, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        print(f"Updated catalog AMFI codes → {args.catalog}")


if __name__ == "__main__":
    main()
