#!/usr/bin/env python3
"""
Fetch equity holdings from Rupeevest and write a NavLab-compatible fund YAML.

Usage:
    python fetch_fund.py <rupeevest_schemecode> [options]

Examples:
    python fetch_fund.py 45529
    python fetch_fund.py 45529 --name "SBI Nifty Next 50 Index Fund Direct Growth"
    python fetch_fund.py 45529 --name "SBI Nifty Next 50 Index Fund Direct Growth" \\
        --amfi 148945 --category "Equity,Index" --plan Direct --option Growth \\
        --type Index --out data/funds/sbi-nifty-next-50-dg.yaml

Notes:
  - Rupeevest schemecode is NOT the AMFI scheme code. They are different ID systems.
    Look up the AMFI code (6-digit) in refined/schemes.csv and pass it with --amfi.
  - Only equity holdings are written. Cash / debt / others are reported but omitted
    so NavLab's stock-based estimator stays clean. Weights are renormalised to ~100%.
  - Use this once or twice a month; portfolios change slowly.
  - Unofficial endpoint — can change or block without notice. For private use only.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import requests
import yaml

URL_TMPL = "https://www.rupeevest.com/home/get_holding_asset?schemecode={code}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}


def slugify(name: str, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (s[:60] or fallback).rstrip("-")


def fetch(schemecode: str) -> dict:
    url = URL_TMPL.format(code=schemecode)
    headers = {**HEADERS, "Referer": f"https://www.rupeevest.com/Mutual-Funds-India/{schemecode}"}
    r = requests.get(url, headers=headers, timeout=30)
    print(f"HTTP {r.status_code}  {url}")
    if r.status_code != 200:
        print("Body (first 300 chars):", r.text[:300])
        sys.exit(1)
    return r.json()


def build_lookups(stock_data_search: list) -> tuple[dict, dict]:
    """Map company name and fincode → NSE symbol."""
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


def extract_holdings(data: dict) -> tuple[list, list, str | None, dict]:
    """
    Returns:
      equity items (dicts with name/symbol/weight),
      missing company names,
      as_of date (YYYY-MM-DD or None),
      cash summary {name: weight}
    """
    by_name, by_fincode = build_lookups(data.get("stock_data_search") or [])
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

    cash = {}
    for h in (data.get("hold_cash") or []) + (data.get("hold_debt") or []) + (data.get("hold_others") or []):
        name = (h.get("compname") or "").strip() or "other"
        try:
            cash[name] = cash.get(name, Decimal(0)) + Decimal(str(h.get("holdpercentage") or 0))
        except Exception:
            pass

    return equity, missing, as_of, cash


def renormalise(items: list, target: Decimal = Decimal("100")) -> list:
    total = sum(i["weight"] for i in items)
    if total <= 0:
        return items
    factor = target / total
    out = []
    for i in items:
        out.append({
            **i,
            "weight": (i["weight"] * factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        })
    # Fix residual so sum is exactly target
    residual = target - sum(i["weight"] for i in out)
    if out and residual != 0:
        out[0]["weight"] = (out[0]["weight"] + residual).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return out


def build_yaml(
    *,
    schemecode: str,
    name: str,
    amfi_code: str | None,
    category: list[str],
    plan: str,
    option: str,
    fund_type: str,
    tags: list[str],
    tax_rule: str,
    equity: list,
    as_of: str,
    missing: list,
    cash: dict,
    raw_eq_sum: Decimal,
) -> dict:
    fid = slugify(name, f"fund-{schemecode}")
    doc = {
        "id": fid,
        "name": name,
        "amfi_code": amfi_code,  # 6-digit AMFI scheme code (NOT Rupeevest schemecode)
        "category": category,
        "fund_type": fund_type,
        "plan": plan,
        "option": option,
        "tags": tags or [plan, option, fund_type],
        "underlying_etf": None,
        "attributes": {
            "rupeevest_schemecode": schemecode,
            "source": "rupeevest",
            "raw_equity_weight_sum": float(raw_eq_sum),
            "cash_and_other": {k: float(v) for k, v in cash.items()},
            "symbols_missing_at_fetch": missing,
        },
        "previous_nav": None,  # fill later via POST /api/funds/{id}/nav or from AMFI daily file
        "tax_rule": tax_rule,
        "costs": [],  # fill manually when known
        "holdings": {
            "as_of": as_of,
            "items": [
                {"name": i["name"], "symbol": i["symbol"], "weight": float(i["weight"])}
                for i in equity
            ],
        },
    }
    return doc


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch Rupeevest holdings → NavLab YAML")
    ap.add_argument("schemecode", help="Rupeevest scheme code (not AMFI code)")
    ap.add_argument("--name", default=None, help="Full fund name (defaults to 'Fund <code>')")
    ap.add_argument("--amfi", default=None, help="AMFI 6-digit scheme code from refined/schemes.csv")
    ap.add_argument("--category", default="Equity,Others", help="Comma path, e.g. Equity,Index")
    ap.add_argument("--plan", default="Direct", choices=["Direct", "Regular", "Other"])
    ap.add_argument("--option", default="Growth", help="Growth / IDCW / ...")
    ap.add_argument("--type", dest="fund_type", default="Active", help="Active / Index / ETF / FoF")
    ap.add_argument("--tags", default="", help="Comma-separated tags")
    ap.add_argument("--tax", default="equity_oriented", help="tax_rule id")
    ap.add_argument("--out", default=None, help="Output YAML path (default: data/funds/<id>.yaml)")
    ap.add_argument("--no-renorm", action="store_true", help="Keep original equity weights (may fail import)")
    args = ap.parse_args()

    name = args.name or f"Fund {args.schemecode}"
    category = [c.strip() for c in args.category.split(",") if c.strip()]
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]

    print(f"Fetching Rupeevest schemecode={args.schemecode} ...")
    data = fetch(args.schemecode)

    equity, missing, as_of, cash = extract_holdings(data)
    if not equity:
        print("ERROR: no equity holdings returned. Wrong code or empty response.")
        sys.exit(1)

    as_of = as_of or date.today().isoformat()
    raw_eq_sum = sum(i["weight"] for i in equity)
    cash_sum = sum(cash.values())

    print(f"Disclosure date (invdate): {as_of}")
    print(f"Equity rows: {len(equity)}   missing symbols: {len(missing)}")
    if missing:
        print("  Missing:")
        for m in missing[:20]:
            print(f"    - {m}")
        if len(missing) > 20:
            print(f"    ... and {len(missing) - 20} more")
    print(f"Raw equity weight sum: {raw_eq_sum:.2f}%")
    print(f"Cash / debt / other:   {cash_sum:.2f}%")
    for k, v in cash.items():
        print(f"    {v:+6.2f}%  {k}")
    print(f"Combined:              {raw_eq_sum + cash_sum:.2f}%")

    if not args.no_renorm:
        equity = renormalise(equity)
        print(f"Renormalised equity weights → sum {sum(i['weight'] for i in equity):.2f}%")
    else:
        print("Kept original weights (--no-renorm)")

    doc = build_yaml(
        schemecode=args.schemecode,
        name=name,
        amfi_code=args.amfi,
        category=category,
        plan=args.plan,
        option=args.option,
        fund_type=args.fund_type,
        tags=tags,
        tax_rule=args.tax,
        equity=equity,
        as_of=as_of,
        missing=missing,
        cash=cash,
        raw_eq_sum=raw_eq_sum,
    )

    out = Path(args.out) if args.out else Path("data/funds") / f"{doc['id']}.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)

    # Safe YAML dump (handles :, #, etc. in names)
    text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False)
    header = (
        f"# Auto-generated by fetch_fund.py from Rupeevest schemecode={args.schemecode}\n"
        f"# as_of={as_of}  equity={len(equity)}  missing_symbols={len(missing)}\n"
        f"# Rupeevest schemecode ≠ AMFI code. Set amfi_code from refined/schemes.csv.\n"
        f"# previous_nav is null — set via POST /api/funds/{doc['id']}/nav or daily AMFI file.\n"
    )
    out.write_text(header + text, encoding="utf-8")

    print(f"\nWrote {out}")
    print(f"  id:        {doc['id']}")
    print(f"  amfi_code: {doc['amfi_code'] or '(not set — fill from schemes.csv)'}")
    print(f"  holdings:  {len(equity)} symbols, as_of={as_of}")
    if missing:
        print(f"  WARNING: {len(missing)} holdings dropped (no NSE symbol). Check names.")
    if abs(raw_eq_sum - 100) > 5:
        print(f"  NOTE: raw equity sum was {raw_eq_sum:.1f}% — large cash/debt/FoF? Review before use.")


if __name__ == "__main__":
    main()
