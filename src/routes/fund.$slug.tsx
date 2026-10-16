import { createFileRoute, Link, notFound } from "@tanstack/react-router";
import { useState, type FormEvent } from "react";
import funds from "@/data/funds.json";
import { estimateNav, getPortfolio, loadHoldings, saveNav, type Portfolio } from "@/lib/holdings.functions";

export const Route = createFileRoute("/fund/$slug")({
  loader: async ({ params }) => {
    const fund = funds.find((f) => f.slug === params.slug);
    if (!fund) throw notFound();
    const portfolio = await getPortfolio({ data: { slug: params.slug } });
    return { fund, portfolio };
  },
  component: FundPage,
  notFoundComponent: () => (
    <main className="mx-auto max-w-3xl px-4 py-10">
      <p>That fund is not on the list.</p>
      <Link to="/" className="text-accent">Back</Link>
    </main>
  ),
});

function FundPage() {
  const { fund, portfolio: initial } = Route.useLoaderData();
  const [portfolio, setPortfolio] = useState<Portfolio | null>(initial);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nav, setNav] = useState(initial?.previousNav || "");
  const [navDate, setNavDate] = useState(initial?.previousNavDate || "");
  const [estimate, setEstimate] = useState<Awaited<ReturnType<typeof estimateNav>> | null>(null);

  async function onLoad(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const next = await loadHoldings({ data: { slug: fund.slug, name: fund.name, schemecode: code.trim() } });
      setPortfolio(next);
      setEstimate(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load holdings");
    } finally {
      setBusy(false);
    }
  }

  async function onSaveNav(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const next = await saveNav({ data: { slug: fund.slug, nav, date: navDate } });
      setPortfolio(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save NAV");
    } finally {
      setBusy(false);
    }
  }

  async function onEstimate() {
    setBusy(true);
    setError(null);
    try {
      setEstimate(await estimateNav({ data: { slug: fund.slug } }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Estimate failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto max-w-3xl px-4 pb-16 pt-6">
      <Link to="/" className="text-sm text-muted">← All funds</Link>
      <h1 className="font-display mt-3 text-4xl text-fg">{fund.name}</h1>
      <p className="mt-1 text-muted">{fund.category}</p>
      <div className="mt-3 flex flex-wrap gap-2 text-xs">
        <span className="rounded-full bg-surface-2 px-2 py-1 text-muted">{fund.option}</span>
        <span className="rounded-full bg-surface-2 px-2 py-1 text-muted">{fund.type}</span>
        <span className="rounded-full bg-surface-2 px-2 py-1 text-muted">{fund.risk} risk</span>
        <span className="rounded-full bg-surface-2 px-2 py-1 text-muted">Exit {fund.exitLoad}%</span>
        <span className="rounded-full bg-surface-2 px-2 py-1 text-muted">TER {fund.expense}%</span>
        <span className="rounded-full bg-surface-2 px-2 py-1 text-muted">AUM ₹{fund.aum.toLocaleString("en-IN")} Cr</span>
      </div>

      {!portfolio ? (
        <form onSubmit={onLoad} className="mt-8 rounded-card border border-line bg-surface p-4">
          <h2 className="font-display text-2xl">First-time setup</h2>
          <p className="mt-1 text-sm text-muted">
            Paste the Rupeevest scheme code (the number in that fund’s Rupeevest URL). Holdings are saved and you won’t need the code again unless you refresh the portfolio.
          </p>
          <input
            value={code}
            onChange={(e) => setCode(e.target.value)}
            inputMode="numeric"
            placeholder="e.g. 45529"
            className="mt-4 min-h-11 w-full rounded-card border border-line bg-bg px-3 text-fg outline-none"
          />
          <button type="submit" disabled={busy || !code.trim()} className="mt-3 min-h-11 rounded-card bg-accent px-4 font-medium text-accent-ink">
            {busy ? "Fetching…" : "Load holdings"}
          </button>
        </form>
      ) : (
        <section className="mt-8">
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div>
              <h2 className="font-display text-2xl">Holdings</h2>
              <p className="text-sm text-muted">
                As of {portfolio.asOf || "unknown"} · code {portfolio.schemecode} · {portfolio.holdings.length} stocks
                {portfolio.missing.length ? ` · ${portfolio.missing.length} names had no symbol` : ""}
              </p>
            </div>
            <button type="button" onClick={onEstimate} disabled={busy} className="min-h-11 rounded-card bg-accent px-4 font-medium text-accent-ink">
              {busy ? "Working…" : "Estimate NAV"}
            </button>
          </div>

          <form onSubmit={onSaveNav} className="mt-4 grid gap-2 rounded-card border border-line bg-surface p-4 sm:grid-cols-[1fr_1fr_auto]">
            <label className="text-sm text-muted">
              Last official NAV
              <input value={nav} onChange={(e) => setNav(e.target.value)} placeholder="19.0755" className="mt-1 min-h-11 w-full rounded-card border border-line bg-bg px-3 text-fg" />
            </label>
            <label className="text-sm text-muted">
              NAV date
              <input value={navDate} onChange={(e) => setNavDate(e.target.value)} placeholder="2026-10-15" className="mt-1 min-h-11 w-full rounded-card border border-line bg-bg px-3 text-fg" />
            </label>
            <button type="submit" disabled={busy} className="min-h-11 self-end rounded-card border border-line px-4 text-fg">Save NAV</button>
          </form>

          {estimate ? (
            <div className="mt-4 rounded-card border border-line bg-surface p-4">
              {estimate.status !== "OK" ? (
                <p className="text-warn">No estimate. Missing live prices for {estimate.missing.slice(0, 8).join(", ")}{estimate.missing.length > 8 ? "…" : ""}.</p>
              ) : (
                <>
                  <p className={`font-display text-4xl ${estimate.changePct >= 0 ? "text-up" : "text-down"}`}>
                    {estimate.changePct >= 0 ? "+" : ""}{estimate.changePct.toFixed(2)}%
                  </p>
                  <p className="mt-1 text-muted">
                    {estimate.estimatedNav != null
                      ? `Estimated NAV ${estimate.estimatedNav.toFixed(4)} from official ${portfolio.previousNav}`
                      : "Change only — save the last official NAV to see an estimated level."}
                  </p>
                </>
              )}
            </div>
          ) : null}

          <div className="mt-4 overflow-hidden rounded-card border border-line">
            <table className="w-full text-left text-sm">
              <thead className="bg-surface-2 text-muted">
                <tr>
                  <th className="px-3 py-2 font-medium">Stock</th>
                  <th className="px-3 py-2 font-medium">Symbol</th>
                  <th className="px-3 py-2 text-right font-medium">Weight</th>
                </tr>
              </thead>
              <tbody>
                {portfolio.holdings.map((h) => (
                  <tr key={h.symbol} className="border-t border-line">
                    <td className="px-3 py-2">{h.name}</td>
                    <td className="px-3 py-2 text-muted">{h.symbol}</td>
                    <td className="px-3 py-2 text-right">{h.weight.toFixed(2)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <details className="mt-4 text-sm text-muted">
            <summary className="cursor-pointer">Replace scheme code</summary>
            <form onSubmit={onLoad} className="mt-2 flex flex-col gap-2 sm:flex-row">
              <input value={code} onChange={(e) => setCode(e.target.value)} placeholder="New Rupeevest code" className="min-h-11 flex-1 rounded-card border border-line bg-bg px-3 text-fg" />
              <button type="submit" disabled={busy || !code.trim()} className="min-h-11 rounded-card border border-line px-4 text-fg">Reload holdings</button>
            </form>
          </details>
        </section>
      )}

      {error ? <p className="mt-4 text-down">{error}</p> : null}
    </main>
  );
}
