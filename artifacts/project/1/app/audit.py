import hashlib
from datetime import datetime, timezone
def _h(prev, ts, symbol, decision, reason):
    return hashlib.sha256(f"{prev}|{ts}|{symbol}|{decision}|{reason}".encode()).hexdigest()
def append(c, symbol, decision, reason):
    """Hash-chained log: each row stores the previous row's hash, so edits are detectable."""
    row = c.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    prev = row["hash"] if row else "GENESIS"
    ts = datetime.now(timezone.utc).isoformat()
    c.execute("INSERT INTO audit_log(ts,symbol,decision,reason,prev_hash,hash) VALUES(?,?,?,?,?,?)",
              (ts, symbol, decision, reason, prev, _h(prev, ts, symbol, decision, reason)))
def verify(c):
    prev = "GENESIS"
    for r in c.execute("SELECT * FROM audit_log ORDER BY id"):
        if r["prev_hash"] != prev or r["hash"] != _h(prev, r["ts"], r["symbol"], r["decision"], r["reason"]):
            return False
        prev = r["hash"]
    return True
