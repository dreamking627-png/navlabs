import asyncio, os, tempfile
from datetime import datetime, timezone, timedelta
from decimal import Decimal as D
import pytest
os.environ["NAVLAB_DB"] = tempfile.mktemp(suffix=".db")
os.environ["NAVLAB_SOURCES"] = "fake"
from app import engine, importer, audit
from app.sources import Router, Source
from app.db import conn
CFG = {"cache_ttl_seconds": 0, "timeout_seconds": 2, "max_age_seconds": 300, "breaker": {"failures": 2, "cooldown_seconds": 60}}
def q(p, prev, src="a"): return {"ltp": D(p), "prev_close": D(prev), "ts": datetime.now(timezone.utc), "source": src}
def test_formula_exact():
    hs = [{"symbol": "A", "weight": "60"}, {"symbol": "B", "weight": "40"}]
    r = engine.compute(hs, {"A": q("102", "100"), "B": q("99", "100")}, prev_nav="10")
    assert r["estimated_change_pct"] == "0.8000" and r["estimated_nav"] == "10.0800" and r["status"] == "OK"
def test_missing_data_refuses():
    r = engine.compute([{"symbol": "A", "weight": "50"}, {"symbol": "B", "weight": "50"}], {"A": q("101", "100")})
    assert r["status"] == "INCOMPLETE" and r["estimated_change_pct"] is None
def test_physics_check():
    assert not engine.validate_quote(q("150", "100"), 20)[0] and engine.validate_quote(q("101", "100"), 20)[0]
def test_import_validation():
    with pytest.raises(importer.ImportFailed): importer.validate_holdings([{"symbol": "A", "weight": 50}])
    with pytest.raises(importer.ImportFailed): importer.validate_holdings([{"symbol": "A", "weight": 50}, {"symbol": "a", "weight": 50}])
def test_real_fund_imports_and_weights():
    c = conn()
    ids = importer.import_all(c)
    assert "groww-nifty-india-defence-etf-fof-dg" in ids
    # Groww has 19 holdings; Jio is skipped (null symbols); SBI may be present if fetched
    groww_n = c.execute(
        "SELECT COUNT(*) n FROM holdings WHERE fund_id=?",
        ("groww-nifty-india-defence-etf-fof-dg",),
    ).fetchone()["n"]
    assert groww_n == 19
    rows = c.execute(
        "SELECT symbol, weight FROM holdings WHERE fund_id=?",
        ("groww-nifty-india-defence-etf-fof-dg",),
    ).fetchall()
    assert importer.validate_holdings([{"symbol": r["symbol"], "weight": r["weight"]} for r in rows]) == D("99.96")
class Bad(Source):
    name = "bad"
    async def fetch(self, s): raise RuntimeError("down")
class Good(Source):
    name = "good"
    async def fetch(self, s): return {x: q("101", "100", "good") for x in s}
def test_fallback_and_breaker():
    r = Router([Bad(), Good()], CFG)
    out, notes = asyncio.run(r.get_quotes(["X"]))
    assert out["X"]["source"] == "good" and any("bad: failed" in n for n in notes)
    asyncio.run(r.get_quotes(["X"])); out, notes = asyncio.run(r.get_quotes(["X"]))
    assert any("circuit open" in n for n in notes)
def test_audit_chain_tamper_detected():
    c = conn(); audit.append(c, "A", "accept", "ok"); audit.append(c, "B", "reject", "bad"); c.commit()
    assert audit.verify(c)
    c.execute("UPDATE audit_log SET reason='edited' WHERE id=1"); c.commit(); assert not audit.verify(c)
def test_api_flow():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as t:
        cats = t.get("/api/categories").json()
        # Find the Groww defence fund specifically (stable across extra funds)
        fid = "groww-nifty-india-defence-etf-fof-dg"
        fund = t.get(f"/api/funds/{fid}").json()
        assert fund["costs"]
        t.post(f"/api/funds/{fid}/nav", json={"date": "2026-10-10", "nav": "10.0000"})
        e = t.post(f"/api/funds/{fid}/estimate").json()
        assert e["status"] == "OK" and e["confidence"] == "DEMO" and len(e["breakdown"]) == 19 and e["estimated_nav"]
        assert t.get(f"/api/estimates/{e['estimate_id']}").status_code == 200
