import { createServerFn } from "@tanstack/react-start";

export type Holding = { name: string; symbol: string; weight: number };
export type Portfolio = {
  slug: string;
  schemecode: string;
  asOf: string | null;
  holdings: Holding[];
  cash: { name: string; weight: number }[];
  missing: string[];
  rawEquitySum: number;
  previousNav: string | null;
  previousNavDate: string | null;
};

const RV = "https://www.rupeevest.com/home/get_holding_asset?schemecode=";

function num(v: unknown) {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : 0;
}

async function sql() {
  const { getSql } = await import("@/lib/db");
  return getSql();
}

function rowToPortfolio(r: Record<string, unknown>): Portfolio {
  return {
    slug: String(r.slug),
    schemecode: String(r.schemecode),
    asOf: r.as_of ? String(r.as_of) : null,
    holdings: JSON.parse(String(r.holdings || "[]")),
    cash: JSON.parse(String(r.cash || "[]")),
    missing: JSON.parse(String(r.missing || "[]")),
    rawEquitySum: num(r.raw_equity_sum),
    previousNav: r.previous_nav ? String(r.previous_nav) : null,
    previousNavDate: r.previous_nav_date ? String(r.previous_nav_date) : null,
  };
}

export const listSaved = createServerFn({ method: "GET" }).handler(async () => {
  const db = await sql();
  const rows = await db`SELECT slug, schemecode, as_of, previous_nav FROM fund_portfolios`;
  return rows.map((r) => ({
    slug: String(r.slug),
    schemecode: String(r.schemecode),
    asOf: r.as_of ? String(r.as_of) : null,
    previousNav: r.previous_nav ? String(r.previous_nav) : null,
  }));
});

export const getPortfolio = createServerFn({ method: "GET" })
  .validator((d: { slug: string }) => {
    if (!d?.slug || !/^[a-z0-9_-]+$/i.test(d.slug)) throw new Error("Bad fund id");
    return d;
  })
  .handler(async ({ data }) => {
    const db = await sql();
    const rows = await db`SELECT * FROM fund_portfolios WHERE slug = ${data.slug}`;
    return rows[0] ? rowToPortfolio(rows[0] as Record<string, unknown>) : null;
  });

async function pullRupeevest(code: string) {
  const res = await fetch(RV + encodeURIComponent(code), {
    headers: {
      "User-Agent": "Mozilla/5.0",
      Accept: "application/json",
      "X-Requested-With": "XMLHttpRequest",
      Referer: `https://www.rupeevest.com/Mutual-Funds-India/${code}`,
    },
  });
  if (!res.ok) throw new Error(`Rupeevest returned ${res.status}`);
  const data = (await res.json()) as {
    hold_eq?: { compname?: string; holdpercentage?: number; invdate?: string; fincode?: number }[];
    hold_cash?: { compname?: string; holdpercentage?: number }[];
    hold_debt?: { compname?: string; holdpercentage?: number }[];
    stock_data_search?: { compname?: string; fincode?: number; stock_search?: string }[];
  };
  const byFin = new Map<string, string>();
  const byName = new Map<string, string>();
  for (const s of data.stock_data_search || []) {
    const parts = (s.stock_search || "").split("|").map((p) => p.trim());
    const sym = parts[2] || "";
    if (!sym) continue;
    if (s.compname) byName.set(s.compname.trim(), sym.toUpperCase());
    if (s.fincode != null) byFin.set(String(s.fincode), sym.toUpperCase());
  }
  const missing: string[] = [];
  const equity: Holding[] = [];
  let asOf: string | null = null;
  for (const h of data.hold_eq || []) {
    const name = (h.compname || "").trim();
    const w = num(h.holdpercentage);
    if (!name || w <= 0) continue;
    if (!asOf && h.invdate) asOf = String(h.invdate).slice(0, 10);
    const sym = byFin.get(String(h.fincode ?? "")) || byName.get(name) || "";
    if (!sym) {
      missing.push(name);
      continue;
    }
    equity.push({ name, symbol: sym, weight: w });
  }
  if (!equity.length) throw new Error("No equity holdings. Wrong code, or this fund only holds an ETF, gold, or debt.");
  const raw = equity.reduce((s, h) => s + h.weight, 0);
  const factor = 100 / raw;
  let running = 0;
  const renorm = equity.map((h, i) => {
    const weight = i === equity.length - 1 ? Math.round((100 - running) * 100) / 100 : Math.round(h.weight * factor * 100) / 100;
    running += weight;
    return { ...h, weight };
  });
  const cash = [...(data.hold_cash || []), ...(data.hold_debt || [])]
    .map((c) => ({ name: (c.compname || "other").trim(), weight: num(c.holdpercentage) }))
    .filter((c) => c.name);
  return { holdings: renorm, missing, asOf, cash, raw };
}

async function mirrorYaml(slug: string, name: string, p: Omit<Portfolio, "previousNav" | "previousNavDate">) {
  try {
    const { mkdir, writeFile } = await import("node:fs/promises");
    const path = await import("node:path");
    const dir = path.resolve(process.cwd(), "artifacts/project/1/data/funds");
    await mkdir(dir, { recursive: true });
    const lines = [
      `id: ${slug}`,
      `name: ${JSON.stringify(name)}`,
      `amfi_code: null`,
      `category: [Equity, Imported]`,
      `fund_type: Active`,
      `plan: Direct`,
      `option: Growth`,
      `tags: []`,
      `underlying_etf: null`,
      `attributes:`,
      `  rupeevest_schemecode: "${p.schemecode}"`,
      `  source: rupeevest`,
      `  raw_equity_weight_sum: ${p.rawEquitySum}`,
      `previous_nav: null`,
      `tax_rule: equity_oriented`,
      `costs: []`,
      `holdings:`,
      `  as_of: "${p.asOf || ""}"`,
      `  items:`,
      ...p.holdings.map(
        (h) => `    - {name: ${JSON.stringify(h.name)}, symbol: ${h.symbol}, weight: ${h.weight}}`,
      ),
      "",
    ];
    await writeFile(path.join(dir, `${slug}.yaml`), lines.join("\n"), "utf8");
  } catch {
    /* preview disk is best-effort; the database row is the source of truth */
  }
}

export const loadHoldings = createServerFn({ method: "POST" })
  .validator((d: { slug: string; name: string; schemecode: string }) => {
    const code = String(d?.schemecode || "").trim();
    if (!d?.slug || !/^[a-z0-9_-]+$/i.test(d.slug)) throw new Error("Bad fund");
    if (!/^\d{3,8}$/.test(code)) throw new Error("Scheme code should be the number from the Rupeevest URL");
    return { slug: d.slug, name: d.name || d.slug, schemecode: code };
  })
  .handler(async ({ data }) => {
    const pulled = await pullRupeevest(data.schemecode);
    const db = await sql();
    const existing = await db`SELECT previous_nav, previous_nav_date FROM fund_portfolios WHERE slug = ${data.slug}`;
    const prev = existing[0] as { previous_nav?: string; previous_nav_date?: string } | undefined;
    await db`
      INSERT INTO fund_portfolios (slug, schemecode, as_of, holdings, cash, missing, raw_equity_sum, previous_nav, previous_nav_date, updated_at)
      VALUES (
        ${data.slug},
        ${data.schemecode},
        ${pulled.asOf},
        ${JSON.stringify(pulled.holdings)},
        ${JSON.stringify(pulled.cash)},
        ${JSON.stringify(pulled.missing)},
        ${String(pulled.raw)},
        ${prev?.previous_nav ?? null},
        ${prev?.previous_nav_date ?? null},
        NOW()
      )
      ON CONFLICT (slug) DO UPDATE SET
        schemecode = EXCLUDED.schemecode,
        as_of = EXCLUDED.as_of,
        holdings = EXCLUDED.holdings,
        cash = EXCLUDED.cash,
        missing = EXCLUDED.missing,
        raw_equity_sum = EXCLUDED.raw_equity_sum,
        updated_at = NOW()
    `;
    const portfolio: Portfolio = {
      slug: data.slug,
      schemecode: data.schemecode,
      asOf: pulled.asOf,
      holdings: pulled.holdings,
      cash: pulled.cash,
      missing: pulled.missing,
      rawEquitySum: pulled.raw,
      previousNav: prev?.previous_nav ? String(prev.previous_nav) : null,
      previousNavDate: prev?.previous_nav_date ? String(prev.previous_nav_date) : null,
    };
    await mirrorYaml(data.slug, data.name, portfolio);
    return portfolio;
  });

export const saveNav = createServerFn({ method: "POST" })
  .validator((d: { slug: string; nav: string; date: string }) => {
    if (!d?.slug) throw new Error("Bad fund");
    if (!/^\d+(\.\d+)?$/.test(d.nav)) throw new Error("NAV must be a number");
    if (!/^\d{4}-\d{2}-\d{2}$/.test(d.date)) throw new Error("Date must be YYYY-MM-DD");
    return d;
  })
  .handler(async ({ data }) => {
    const db = await sql();
    const rows = await db`
      UPDATE fund_portfolios
      SET previous_nav = ${data.nav}, previous_nav_date = ${data.date}, updated_at = NOW()
      WHERE slug = ${data.slug}
      RETURNING *
    `;
    if (!rows[0]) throw new Error("Load holdings before saving a NAV");
    return rowToPortfolio(rows[0] as Record<string, unknown>);
  });

async function quote(symbol: string) {
  const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}.NS?interval=1d&range=5d`;
  const res = await fetch(url, {
    headers: { "User-Agent": "Mozilla/5.0", Accept: "application/json" },
  });
  if (!res.ok) throw new Error(`quote ${symbol} HTTP ${res.status}`);
  const j = (await res.json()) as {
    chart?: { result?: { meta?: { regularMarketPrice?: number; chartPreviousClose?: number; previousClose?: number } }[] };
  };
  const m = j.chart?.result?.[0]?.meta;
  const ltp = m?.regularMarketPrice;
  const prev = m?.chartPreviousClose ?? m?.previousClose;
  if (!ltp || !prev) throw new Error(`empty quote ${symbol}`);
  return { ltp, prev };
}

export const estimateNav = createServerFn({ method: "POST" })
  .validator((d: { slug: string }) => d)
  .handler(async ({ data }) => {
    const db = await sql();
    const rows = await db`SELECT * FROM fund_portfolios WHERE slug = ${data.slug}`;
    if (!rows[0]) throw new Error("No holdings stored for this fund yet");
    const p = rowToPortfolio(rows[0] as Record<string, unknown>);
    const quotes = new Map<string, { ltp: number; prev: number }>();
    const failed: string[] = [];
    const queue = [...p.holdings];
    const workers = Array.from({ length: 6 }, async () => {
      for (;;) {
        const h = queue.shift();
        if (!h) return;
        try {
          quotes.set(h.symbol, await quote(h.symbol));
        } catch {
          failed.push(h.symbol);
        }
      }
    });
    await Promise.all(workers);
    if (failed.length) {
      return {
        status: "INCOMPLETE" as const,
        changePct: null as number | null,
        estimatedNav: null as number | null,
        mapped: 0,
        missing: failed,
        rows: [] as { symbol: string; name: string; weight: number; changePct: number | null; contribution: number | null }[],
      };
    }
    let change = 0;
    const breakdown = p.holdings.map((h) => {
      const q = quotes.get(h.symbol)!;
      const pct = ((q.ltp - q.prev) / q.prev) * 100;
      const contribution = (h.weight * pct) / 100;
      change += contribution;
      return { symbol: h.symbol, name: h.name, weight: h.weight, changePct: pct, contribution };
    });
    const prevNav = p.previousNav ? Number(p.previousNav) : null;
    return {
      status: "OK" as const,
      changePct: change,
      estimatedNav: prevNav ? prevNav * (1 + change / 100) : null,
      mapped: 100,
      missing: [] as string[],
      rows: breakdown,
    };
  });
