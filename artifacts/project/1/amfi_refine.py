#!/usr/bin/env python3
"""
amfi_refine.py - refine a raw AMFI NAV text file into clean CSV + JSON.
Standalone: Python 3.8+, standard library only. Does not touch any other project.

Usage:
    python amfi_refine.py raw_nav.txt                 # output goes to ./refined/
    python amfi_refine.py raw1.txt raw2.txt -o out    # several files (later NAV date wins)
    python amfi_refine.py my_raw_folder -o out        # every *.txt in a folder
    python amfi_refine.py --selftest                  # verify the tool on built-in samples

Reads line by line (constant memory for reading), so files with lakhs of lines are fine.
Understands:  code;name;plan;option;isin1;isin2;nav;date   and the classic   code;isin1;isin2;name;nav;date
Plain lines are either category lines  "Open Ended Schemes(Equity Scheme - Large Cap Fund)"  or fund-house names.
Outputs: schemes.csv, schemes.json, report.txt
"""
import argparse, csv, glob, json, os, re, sys, time
from collections import Counter
from datetime import date, timedelta

MON = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
CAT_RE = re.compile(r"^(Open Ended|Close Ended|Closed Ended|Interval Fund)\s*Schemes?\s*\((.*)\)\s*$", re.I)
DATE_RE = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")
NAV_RE = re.compile(r"^\d+(\.\d+)?$")
ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
NOISE = re.compile(r"\b(DIRECT|REGULAR|PLAN|GROWTH|OPTION|IDCW|DIVIDEND|PAYOUT|REINVESTMENT|BONUS|OF|THE|DAILY|WEEKLY|MONTHLY|QUARTERLY|ANNUAL|FORTNIGHTLY|HALF|YEARLY)\b")
FIELDS = ["code", "name", "base_name", "amc", "scheme_type", "category", "sub_category", "plan", "option", "fund_type",
          "isin_1", "isin_2", "nav", "nav_date", "stale", "issues"]


def iso(s):
    m = DATE_RE.match(s.strip())
    if not m or m[2].lower() not in MON:
        return None
    try:
        return date(int(m[3]), MON[m[2].lower()], int(m[1])).isoformat()      # rejects 31-Feb etc.
    except ValueError:
        return None


def fund_type(name):
    u = name.upper()
    if "FOF" in u or "FUND OF FUND" in u:
        return "FoF"
    if re.search(r"\bETF\b", u) or "EXCHANGE TRADED" in u:
        return "ETF"
    return "Index" if "INDEX" in u else "Active"


def norm_plan(p, name):
    u = (p + " " + name).upper()
    return "Direct" if "DIRECT" in u else "Regular" if "REGULAR" in u else "Other"


def norm_opt(o, name):
    u = (o or name).upper()
    if "IDCW" in u or "DIVIDEND" in u:
        return "IDCW"
    if "BONUS" in u:
        return "Bonus"
    if "GROWTH" in u:
        return "Growth"
    return o.title() if o else "Other"


def base_name(name):
    """Name without plan/option words, so Direct/Regular/Growth/IDCW variants of one fund share a key."""
    u = re.sub(r"[^A-Z0-9 ]+", " ", name.upper())
    return re.sub(r"\s+", " ", NOISE.sub(" ", u)).strip()


def parse(lines, st, delim=";"):
    """Yield one record (dict) per scheme row. Counts everything it cannot use in st."""
    amc = stype = cat = sub = None
    for raw in lines:
        st["lines"] += 1
        line = raw.strip().lstrip("\ufeff")
        if not line:
            continue
        if len(st["first"]) < 3:
            st["first"].append(line[:100])
        if delim not in line:
            m = CAT_RE.match(line)
            if m:
                stype = m[1].title()
                cat, _, sub = m[2].partition(" - ")
                cat, sub = cat.strip(), sub.strip()
                st["category_lines"] += 1
            else:
                amc = line
                st["amc_lines"] += 1
            continue
        f = [x.strip().strip('"') for x in line.split(delim)]
        if not f[0].isdigit():
            if not f[0].lower().startswith("scheme code"):
                skip(st, "not a scheme row", line)
            continue
        if len(f) == 6:
            code, i1, i2, name, nav, dt = f
            plan = opt = ""
        elif len(f) >= 8:
            code, name, plan, opt, i1, i2, nav, dt = f[:8]
        else:
            skip(st, f"unexpected field count {len(f)}", line)
            continue
        d = iso(dt)
        if not d or not name:
            skip(st, "bad date or empty name", line)
            continue
        issues = []
        navv = nav if NAV_RE.match(nav) and float(nav) > 0 else ""
        if not navv:
            issues.append("nav_missing")
        for lab, v in (("isin_1", i1), ("isin_2", i2)):
            if v and not ISIN_RE.match(v):
                issues.append("bad_" + lab)
        if not amc:
            issues.append("no_amc")
        yield {"code": code, "name": name, "base_name": base_name(name), "amc": amc or "", "scheme_type": stype or "",
               "category": cat or "", "sub_category": sub or "", "plan": norm_plan(plan, name), "option": norm_opt(opt, name),
               "fund_type": fund_type(name), "isin_1": i1, "isin_2": i2, "nav": navv, "nav_date": d, "stale": False,
               "issues": ";".join(issues)}


def skip(st, why, line):
    st["skipped_count"] += 1
    st["skipped_reasons"][why] += 1
    if len(st["skipped"]) < 40:
        st["skipped"].append(f"line {st['lines']}: {why}: {line[:90]}")


def new_stats():
    return {"lines": 0, "rows": 0, "amc_lines": 0, "category_lines": 0, "skipped_count": 0, "skipped": [],
            "skipped_reasons": Counter(), "duplicates": 0, "older_ignored": 0, "first": [], "files": []}


def sniff(path):
    """Detect text encoding (UTF-8 / UTF-16) and the column separator (; tab | ,) from the start of the file."""
    with open(path, "rb") as fb:
        head = fb.read(400000)
    enc = "utf-16" if head[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
    text = head.decode(enc, errors="replace").splitlines()
    best, bn = ";", 0
    for d in (";", "\t", "|", ","):
        n = sum(1 for l in text if re.match(r'^\s*"?\d+"?\s*' + re.escape(d), l))
        if n > bn:
            best, bn = d, n
    return enc, best


def collect(paths, st, quiet=False):
    """Read all files; keep one record per scheme code (newest NAV date wins, ties -> later file)."""
    best = {}
    for p in paths:
        enc, delim = sniff(p)
        st["files"].append(f"{os.path.basename(p)}: encoding {enc}, separator {delim!r}")
        with open(p, encoding=enc, errors="replace") as fh:
            for rec in parse(fh, st, delim):
                st["rows"] += 1
                old = best.get(rec["code"])
                if old is not None:
                    st["duplicates"] += 1
                    if rec["nav_date"] < old["nav_date"]:
                        st["older_ignored"] += 1
                        continue
                best[rec["code"]] = rec
                if not quiet and st["rows"] % 100000 == 0:
                    print(f"  ... {st['rows']:,} scheme rows read", flush=True)
    return best


def mark_stale(best, days=10):
    """Schemes whose NAV date is far behind the newest date in the file (closed/matured/inactive)."""
    if not best:
        return None, 0
    newest = max(r["nav_date"] for r in best.values())
    limit = (date.fromisoformat(newest) - timedelta(days=days)).isoformat()
    n = 0
    for r in best.values():
        if r["nav_date"] < limit:
            r["stale"] = True
            r["issues"] = (r["issues"] + ";" if r["issues"] else "") + "stale_nav"
            n += 1
    return newest, n


def write_outputs(best, outdir):
    os.makedirs(outdir, exist_ok=True)
    recs = sorted(best.values(), key=lambda r: (r["amc"], r["name"]))
    with open(os.path.join(outdir, "schemes.csv"), "w", encoding="utf-8-sig", newline="") as fc:
        w = csv.DictWriter(fc, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(recs)
    with open(os.path.join(outdir, "schemes.json"), "w", encoding="utf-8") as fj:
        fj.write("[\n")
        for i, r in enumerate(recs):
            fj.write(("," if i else "") + json.dumps(r, ensure_ascii=False) + "\n")
        fj.write("]\n")
    return recs


def report(recs, st, newest, stale, secs):
    c = lambda k: Counter(r[k] for r in recs)
    top = Counter(r["amc"] or "(unknown)" for r in recs).most_common(10)
    issue = Counter(i for r in recs for i in r["issues"].split(";") if i)
    dates = Counter(r["nav_date"] for r in recs).most_common(3)
    L = ["AMFI REFINE REPORT", "=" * 40,
         f"Lines read:            {st['lines']:,}",
         f"Scheme rows found:     {st['rows']:,}",
         f"Unique schemes saved:  {len(recs):,}",
         f"Duplicate codes:       {st['duplicates']:,}  (older-date copies ignored: {st['older_ignored']:,})",
         f"Fund houses:           {len({r['amc'] for r in recs if r['amc']}):,}",
         f"Category lines seen:   {st['category_lines']:,}",
         f"Newest NAV date:       {newest}", f"Most common dates:     {dates}",
         f"Stale schemes (NAV >10 days older than newest): {stale:,}", "",
         f"Plans:   {dict(c('plan'))}", f"Options: {dict(c('option'))}", f"Types:   {dict(c('fund_type'))}", "",
         f"Rows with problems:    {sum(1 for r in recs if r['issues']):,}   {dict(issue)}", "",
         "Top fund houses: " + ", ".join(f"{a} ({n})" for a, n in top), "",
         f"Lines skipped: {st['skipped_count']:,}   {dict(st['skipped_reasons'])}"]
    L += ["  " + s for s in st["skipped"]]
    if st["skipped_count"] > len(st["skipped"]):
        L.append(f"  ... and {st['skipped_count'] - len(st['skipped']):,} more")
    L += ["", f"Time taken: {secs:.1f}s"]
    return "\n".join(L)


def find_inputs(args):
    out = []
    for a in args:
        if os.path.isdir(a):
            out += sorted(glob.glob(os.path.join(a, "*.txt")))
        else:
            out += sorted(glob.glob(a)) or [a]
    empty = [p for p in out if os.path.isfile(p) and os.path.getsize(p) == 0]
    if empty:
        sys.exit("File is empty (0 bytes): " + ", ".join(empty) + "\nOpen it in Notepad and check that your data was pasted and saved.")
    missing = [p for p in out if not os.path.isfile(p)]
    if missing or not out:
        sys.exit("Input not found: " + (", ".join(missing) if missing else "no input given. Run with --help"))
    return out


SAMPLE = """Scheme Code;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date

Open Ended Schemes(Equity Scheme - Large Cap Fund)

Angel One Mutual Fund
153529;ANGEL ONE NIFTY 50 INDEX FUND-DIRECT-GROWTH;Direct Plan;Growth;INF1J2R01072;;9.3648;13-Oct-2026
153530;ANGEL ONE NIFTY 50 INDEX FUND-REGULAR-GROWTH;Regular Plan;Growth;BADISIN;;N.A.;13-Oct-2026
bad line;with;semicolons
153999;too;few;fields
153998;Name;Direct Plan;Growth;INF1J2R01072;;5.1;31-Feb-2026

Mirae Asset Mutual Fund
119551;INF209KA12Z1;INF209KA13Z9;Mirae Asset Focused Fund - Direct Plan - IDCW;26.811;09-Oct-2026
119551;INF209KA12Z1;INF209KA13Z9;Mirae Asset Focused Fund - Direct Plan - IDCW;20.000;01-Oct-2026
100001;INF209KA12Z2;;Old Matured Fund - Regular Plan - Growth;11.0;20-Aug-2026
"""


def selftest():
    import tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, "s.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write(SAMPLE)
    st = new_stats()
    best = collect([p], st, quiet=True)
    newest, stale = mark_stale(best)
    recs = write_outputs(best, d)
    A = best["153529"]
    checks = [
        (len(best) == 4 and st["rows"] == 5, "counts"),
        (st["skipped_count"] == 3, "skips: junk line, too few fields, impossible date 31-Feb"),
        ((A["amc"], A["category"], A["sub_category"], A["plan"], A["option"], A["fund_type"], A["nav"], A["nav_date"]) ==
         ("Angel One Mutual Fund", "Equity Scheme", "Large Cap Fund", "Direct", "Growth", "Index", "9.3648", "2026-10-13"), "layout A fields"),
        (best["153530"]["nav"] == "" and "nav_missing" in best["153530"]["issues"] and "bad_isin_1" in best["153530"]["issues"], "N.A. NAV and bad ISIN flagged"),
        (best["119551"]["nav"] == "26.811" and st["older_ignored"] == 1, "newest date wins on duplicate"),
        ((best["119551"]["option"], best["119551"]["plan"]) == ("IDCW", "Direct"), "classic layout"),
        (best["100001"]["stale"] is True and newest == "2026-10-13" and stale == 1, "stale detection"),
        (base_name("ANGEL ONE NIFTY 50 INDEX FUND-DIRECT-GROWTH") == base_name("ANGEL ONE NIFTY 50 INDEX FUND-REGULAR-GROWTH"), "base_name groups variants"),
        (os.path.getsize(os.path.join(d, "schemes.csv")) > 0 and len(json.load(open(os.path.join(d, "schemes.json"), encoding="utf-8"))) == 4, "csv + json written and valid"),
    ]
    row = "153529;ANGEL ONE NIFTY 50 INDEX FUND-DIRECT-GROWTH;Direct Plan;Growth;INF1J2R01072;;9.3648;13-Oct-2026\n"
    for label, text, enc in (("tab-separated", row.replace(";", "\t"), "utf-8"), ("pipe-separated", row.replace(";", "|"), "utf-8"),
                             ("UTF-16 file (PowerShell/Notepad 'Unicode')", "Angel One Mutual Fund\n" + row, "utf-16")):
        q = os.path.join(d, "x.txt")
        with open(q, "w", encoding=enc) as f:
            f.write(text)
        s2 = new_stats()
        b2 = collect([q], s2, quiet=True)
        checks.append((len(b2) == 1 and b2["153529"]["nav"] == "9.3648", label))
    for ok, name in checks:
        print(("PASS  " if ok else "FAIL  ") + name)
    sys.exit(0 if all(ok for ok, _ in checks) else 1)


def main():
    ap = argparse.ArgumentParser(description="Refine a raw AMFI NAV text file into clean CSV + JSON.")
    ap.add_argument("inputs", nargs="*", help="raw .txt file(s), glob(s) or folder(s)")
    ap.add_argument("-o", "--out", default="refined", help="output folder (default: refined)")
    ap.add_argument("--stale-days", type=int, default=10, help="mark schemes whose NAV is this many days older than the newest (default 10)")
    ap.add_argument("--selftest", action="store_true", help="run built-in checks and exit")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    paths = find_inputs(a.inputs)
    t0 = time.time()
    st = new_stats()
    print(f"Reading {len(paths)} file(s)...")
    best = collect(paths, st)
    if not best:
        print("\n".join(st["files"]))
        print(f"Lines read: {st['lines']:,}   skipped: {st['skipped_count']:,}   plain text lines: {st['amc_lines'] + st['category_lines']:,}")
        print("First lines of the file:")
        for l in st["first"]:
            print("   |", l)
        sys.exit("\nNo scheme rows found. A scheme row must start with the scheme code (digits) followed by a separator, "
                 "for example:  153529;FUND NAME;Direct Plan;Growth;INF...;;9.3648;13-Oct-2026\n"
                 "Check that the file really contains rows like that.")
    newest, stale = mark_stale(best, a.stale_days)
    recs = write_outputs(best, a.out)
    rep = report(recs, st, newest, stale, time.time() - t0)
    rep = rep.replace("Lines read:", "Files: " + " | ".join(st["files"]) + "\nLines read:", 1)
    with open(os.path.join(a.out, "report.txt"), "w", encoding="utf-8") as f:
        f.write(rep + "\n")
    print(rep)
    print(f"\nWrote: {os.path.join(a.out, 'schemes.csv')}, schemes.json, report.txt")


if __name__ == "__main__":
    main()