"""Calculation engine: Decimal only. Refuses to guess when data is missing."""
from decimal import Decimal as D, ROUND_HALF_UP
def _q(x, p="0.0001"): return str(x.quantize(D(p), ROUND_HALF_UP))
def validate_quote(q, max_abs_pct):
    if q["ltp"] <= 0 or q["prev_close"] <= 0: return False, "non-positive price"
    if abs((q["ltp"] - q["prev_close"]) / q["prev_close"] * 100) > D(str(max_abs_pct)): return False, "change beyond physics limit"
    return True, "ok"
def compute(holdings, quotes, prev_nav=None, sanity_pct=10):
    rows, missing, mapped, total, change = [], [], D(0), D(0), D(0)
    for h in holdings:
        w, q = D(str(h["weight"])), quotes.get(h["symbol"])
        total += w
        if not q:
            missing.append(h["symbol"])
            rows.append({"symbol": h["symbol"], "name": h.get("name"), "weight": _q(w, "0.01"), "price": None, "change": None, "change_pct": None, "contribution": None, "source": None})
            continue
        chg = q["ltp"] - q["prev_close"]; pct = chg / q["prev_close"] * 100; con = w * pct / 100
        mapped += w; change += con
        rows.append({"symbol": h["symbol"], "name": h.get("name"), "weight": _q(w, "0.01"), "price": _q(q["ltp"], "0.01"), "change": _q(chg, "0.01"),
                     "change_pct": _q(pct, "0.01"), "contribution": _q(con), "source": q["source"]})
    res = {"breakdown": rows, "mapped_weight": _q(mapped, "0.01"), "total_weight": _q(total, "0.01"), "missing": missing,
           "estimated_change_pct": None, "estimated_nav": None, "status": "OK", "warnings": []}
    if missing:
        res["status"] = "INCOMPLETE"; res["warnings"].append("Missing trusted prices for: " + ", ".join(missing) + ". No estimate produced."); return res
    if abs(change) > D(str(sanity_pct)):
        res["status"] = "REJECTED"; res["warnings"].append("Estimate failed sanity check."); return res
    res["estimated_change_pct"] = _q(change)
    if prev_nav: res["estimated_nav"] = _q(D(str(prev_nav)) * (1 + change / 100))
    else: res["warnings"].append("No previous official NAV stored: showing change % only.")
    return res
def confidence(quotes, status):
    if status != "OK": return "NONE"
    srcs = {q["source"] for q in quotes.values()}
    return "DEMO" if "fake" in srcs else ("HIGH" if len(srcs) == 1 else "DEGRADED")
