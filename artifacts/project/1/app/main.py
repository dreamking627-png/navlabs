import json, time
from datetime import datetime, timezone, date
from decimal import Decimal as D
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from . import audit, engine, importer
from .config import ROOT, settings
from .db import conn
from .sources import build_router, market_open
app = FastAPI(title="NavLab")
router = build_router()
@app.on_event("startup")
def startup():
    c = conn(); importer.import_all(c); c.close()
def _fund_or_404(c, fid):
    f = c.execute("SELECT * FROM funds WHERE id=?", (fid,)).fetchone()
    if not f: raise HTTPException(404, "fund not found")
    return f
def _holdings(c, fid):
    a = c.execute("SELECT MAX(as_of) m FROM holdings WHERE fund_id=?", (fid,)).fetchone()["m"]
    rows = c.execute("SELECT h.symbol,h.weight,s.name FROM holdings h JOIN stocks s ON s.symbol=h.symbol WHERE fund_id=? AND as_of=? ORDER BY CAST(h.weight AS REAL) DESC", (fid, a)).fetchall()
    return a, [dict(r) for r in rows]
def _latest_nav(c, fid):
    r = c.execute("SELECT date,nav FROM nav_history WHERE fund_id=? ORDER BY date DESC LIMIT 1", (fid,)).fetchone()
    return dict(r) if r else None
@app.get("/api/categories")
def categories():
    c = conn(); cats = [dict(r) for r in c.execute("SELECT * FROM categories")]
    funds = [dict(id=r["id"], name=r["name"], category_id=r["category_id"], fund_type=r["fund_type"], tags=json.loads(r["tags"] or "[]")) for r in c.execute("SELECT * FROM funds")]
    def node(cat): return {"id": cat["id"], "name": cat["name"], "funds": [f for f in funds if f["category_id"] == cat["id"]], "children": [node(x) for x in cats if x["parent_id"] == cat["id"]]}
    return [node(x) for x in cats if x["parent_id"] is None]
@app.get("/api/funds/{fid}")
def fund(fid: str):
    c = conn(); f = _fund_or_404(c, fid)
    rows = c.execute("SELECT * FROM fund_facts WHERE fund_id=? AND effective_from<=? ORDER BY effective_from DESC", (fid, date.today().isoformat())).fetchall()
    seen, costs = set(), []
    for r in rows:
        if r["key"] not in seen: seen.add(r["key"]); costs.append(dict(key=r["key"], value=json.loads(r["value"]), note=r["note"], effective_from=r["effective_from"]))
    t = c.execute("SELECT data FROM tax_rules WHERE id=? ORDER BY effective_from DESC LIMIT 1", (f["tax_rule"],)).fetchone()
    cat = c.execute("SELECT name FROM categories WHERE id=?", (f["category_id"],)).fetchone()
    return {"id": f["id"], "name": f["name"], "amfi_code": f["amfi_code"], "category": cat["name"] if cat else None, "plan": f["plan"], "option": f["option"],
            "fund_type": f["fund_type"], "tags": json.loads(f["tags"] or "[]"), "underlying": json.loads(f["underlying"] or "null"),
            "attributes": json.loads(f["attributes"] or "{}"), "costs": costs, "tax": json.loads(t["data"]) if t else None, "last_nav": _latest_nav(c, fid)}
@app.get("/api/funds/{fid}/holdings")
def holdings(fid: str):
    c = conn(); _fund_or_404(c, fid); a, h = _holdings(c, fid); return {"as_of": a, "holdings": h}
class NavIn(BaseModel):
    date: str
    nav: str
@app.post("/api/funds/{fid}/nav")
def set_nav(fid: str, body: NavIn):
    c = conn(); _fund_or_404(c, fid)
    c.execute("INSERT OR REPLACE INTO nav_history VALUES(?,?,?)", (fid, body.date, body.nav)); c.commit(); return {"ok": True}
@app.post("/api/funds/{fid}/estimate")
async def estimate(fid: str):
    c = conn(); f = _fund_or_404(c, fid); a, hs = _holdings(c, fid); cfg = settings()
    und = json.loads(f["underlying"] or "null") or {}
    etf_sym = und.get("symbol")
    raw, notes = await router.get_quotes([h["symbol"] for h in hs] + ([etf_sym] if etf_sym else []))
    good = {}
    for s, q in raw.items():
        ok, why = engine.validate_quote(q, cfg["max_abs_change_pct"])
        audit.append(c, s, "accept" if ok else "reject", f"{q['source']}: {why}")
        if ok: good[s] = q
    nav = _latest_nav(c, fid)
    res = engine.compute(hs, good, nav["nav"] if nav else None, cfg["sanity_max_abs_nav_change_pct"])
    res["etf_check"] = None
    if etf_sym in good and res["estimated_change_pct"] is not None:
        e = good[etf_sym]; em = (e["ltp"] - e["prev_close"]) / e["prev_close"] * 100; gap = abs(em - D(res["estimated_change_pct"]))
        res["etf_check"] = {"etf_change_pct": str(em.quantize(D("0.01"))), "gap_pct": str(gap.quantize(D("0.01")))}
        if gap > 1: res["warnings"].append("Estimate differs from live ETF move by more than 1%.")
    res.update(fund_id=fid, ts=datetime.now(timezone.utc).isoformat(), previous_nav=nav, holdings_as_of=a, source_notes=notes,
               confidence=engine.confidence(good, res["status"]), market_open=market_open())
    audit.append(c, fid, "estimate", f"status={res['status']} change={res['estimated_change_pct']}")
    res["estimate_id"] = c.execute("INSERT INTO estimates(fund_id,ts,result) VALUES(?,?,?)", (fid, res["ts"], json.dumps(res))).lastrowid
    c.commit(); return res
@app.get("/api/estimates/{eid}")
def get_estimate(eid: int):
    r = conn().execute("SELECT result FROM estimates WHERE id=?", (eid,)).fetchone()
    if not r: raise HTTPException(404, "not found")
    return json.loads(r["result"])
@app.get("/api/health")
def health():
    c = conn()
    return {"ok": True, "sources": [s.name for s in router.sources], "circuit_open": [k for k, v in router.open_until.items() if v > time.time()],
            "audit_chain_valid": audit.verify(c), "funds": c.execute("SELECT COUNT(*) n FROM funds").fetchone()["n"]}
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
@app.get("/")
def index(): return FileResponse(ROOT / "static/index.html")
