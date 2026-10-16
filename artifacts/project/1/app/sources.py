"""Price sources = plug-in adapters. A source returns {symbol: {ltp, prev_close, ts, source}} (Decimals, UTC ts)."""
import asyncio, hashlib, os, time
from datetime import datetime, timezone, timedelta
from decimal import Decimal as D
import httpx
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36", "Accept": "application/json"}
async def getj(cl, url, **kw):
    """GET and parse JSON; on failure raise with the HTTP status and the start of the body."""
    r = await cl.get(url, **kw)
    try: return r.json()
    except Exception: raise RuntimeError(f"HTTP {r.status_code}, not JSON: {r.text[:120]!r}")
async def safe(fn, sym, errs):
    """Run one per-symbol request; remember the real error instead of hiding it."""
    try: await fn(sym)
    except Exception as e: errs.append(f"{sym}: {type(e).__name__}: {str(e)[:120]}")
class Source:
    name = "base"
    async def fetch(self, symbols): raise NotImplementedError
class YFinance(Source):
    """yfinance library (handles Yahoo cookies). It gives no quote time, so ts = fetch time and staleness can't be checked."""
    name = "yfinance"
    async def fetch(self, symbols):
        import yfinance as yf
        out, errs, sem = {}, [], asyncio.Semaphore(6)
        def sync(s):
            fi = yf.Ticker(f"{s}.NS").fast_info
            return fi["last_price"], fi["previous_close"]
        async def one(s):
            async with sem: lp, pc = await asyncio.to_thread(sync, s)
            if not lp or not pc: raise RuntimeError("empty price")
            out[s] = {"ltp": D(str(round(float(lp), 2))), "prev_close": D(str(round(float(pc), 2))), "ts": datetime.now(timezone.utc), "source": self.name}
        await asyncio.gather(*[safe(one, s, errs) for s in symbols])
        if symbols and not out: raise RuntimeError(f"all {len(symbols)} requests failed; first error: {errs[0]}")
        return out
class Yahoo(Source):
    """UNVERIFIED unofficial endpoint. One request per symbol, run concurrently."""
    name = "yahoo"
    async def fetch(self, symbols):
        out, errs = {}, []
        async with httpx.AsyncClient(headers=UA, timeout=8) as cl:
            async def one(s):
                j = await getj(cl, f"https://query1.finance.yahoo.com/v8/finance/chart/{s}.NS", params={"interval": "1m", "range": "1d"})
                m = j["chart"]["result"][0]["meta"]
                out[s] = {"ltp": D(str(m["regularMarketPrice"])), "prev_close": D(str(m.get("chartPreviousClose") or m["previousClose"])),
                          "ts": datetime.fromtimestamp(m["regularMarketTime"], timezone.utc), "source": self.name}
            await asyncio.gather(*[safe(one, s, errs) for s in symbols])
        if symbols and not out: raise RuntimeError(f"all {len(symbols)} requests failed; first error: {errs[0]}")
        return out
class NSE(Source):
    """UNVERIFIED unofficial NSE website JSON; needs cookies from the home page first."""
    name = "nse"
    async def fetch(self, symbols):
        out, errs = {}, []
        async with httpx.AsyncClient(headers={**UA, "Referer": "https://www.nseindia.com/"}, timeout=8, follow_redirects=True) as cl:
            await cl.get("https://www.nseindia.com/")
            async def one(s):
                p = (await getj(cl, "https://www.nseindia.com/api/quote-equity", params={"symbol": s}))["priceInfo"]
                out[s] = {"ltp": D(str(p["lastPrice"])), "prev_close": D(str(p["previousClose"])), "ts": datetime.now(timezone.utc), "source": self.name}
            await asyncio.gather(*[safe(one, s, errs) for s in symbols])
        if symbols and not out: raise RuntimeError(f"all {len(symbols)} requests failed; first error: {errs[0]}")
        return out
class Fake(Source):
    """Deterministic demo data for testing without internet. Never use for real decisions."""
    name = "fake"
    async def fetch(self, symbols):
        out = {}
        for s in symbols:
            prev = D(100 + int(hashlib.sha256(s.encode()).hexdigest(), 16) % 4000) / 2
            pct = D((int(hashlib.sha256((s + str(int(time.time() // 60))).encode()).hexdigest(), 16) % 401) - 200) / 100
            out[s] = {"ltp": (prev * (1 + pct / 100)).quantize(D("0.01")), "prev_close": prev, "ts": datetime.now(timezone.utc), "source": self.name}
        return out
REGISTRY = {"yfinance": YFinance, "yahoo": Yahoo, "nse": NSE, "fake": Fake}   # new source = new class + one line here
def market_open(now=None):
    ist = (now or datetime.now(timezone.utc)) + timedelta(hours=5, minutes=30)
    return ist.weekday() < 5 and (9, 15) <= (ist.hour, ist.minute) <= (15, 30)
class Router:
    def __init__(self, sources, cfg):
        self.sources, self.cfg = sources, cfg
        self.cache, self.fails, self.open_until = {}, {}, {}
    def _fresh(self, q):
        return (not market_open()) or (datetime.now(timezone.utc) - q["ts"]).total_seconds() <= self.cfg["max_age_seconds"]
    async def get_quotes(self, symbols):
        out, notes, ttl = {}, [], self.cfg["cache_ttl_seconds"]
        for s in self.sources:
            need = [x for x in symbols if x not in out]
            if not need: break
            if self.open_until.get(s.name, 0) > time.time():
                notes.append(f"{s.name}: skipped (circuit open)"); continue
            got, todo = {}, []
            for x in need:
                hit = self.cache.get((s.name, x))
                if hit and time.time() - hit[0] < ttl: got[x] = hit[1]
                else: todo.append(x)
            if todo:
                try:
                    res = await asyncio.wait_for(s.fetch(todo), self.cfg["timeout_seconds"])
                    self.fails[s.name] = 0
                    for k, v in res.items(): self.cache[(s.name, k)] = (time.time(), v); got[k] = v
                except Exception as e:
                    self.fails[s.name] = self.fails.get(s.name, 0) + 1
                    if self.fails[s.name] >= self.cfg["breaker"]["failures"]:
                        self.open_until[s.name] = time.time() + self.cfg["breaker"]["cooldown_seconds"]
                    notes.append(f"{s.name}: failed - {type(e).__name__}: {str(e)[:200]}")
            for k, v in got.items():
                if self._fresh(v): out[k] = v
                else: notes.append(f"{s.name}: stale quote for {k}")
        return out, notes
def build_router(names=None):
    from .config import settings
    cfg = settings()
    env = os.environ.get("NAVLAB_SOURCES")
    names = names or (env.split(",") if env else cfg["order"])
    return Router([REGISTRY[n.strip()]() for n in names], cfg)