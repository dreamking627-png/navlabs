import { createFileRoute, Link } from "@tanstack/react-router";
import { useMemo, useState, type ReactNode } from "react";
import { Search } from "lucide-react";
import funds from "@/data/funds.json";
import { listSaved } from "@/lib/holdings.functions";

type Fund = (typeof funds)[number];

export const Route = createFileRoute("/")({
  loader: () => listSaved(),
  component: Home,
});

const RISKS = ["All", "Low", "Moderate", "High", "Very high"] as const;
const TYPES = ["All", "Active", "Index", "FoF"] as const;
const OPTIONS = ["All", "Growth", "IDCW"] as const;
const EXITS = [
  { id: "All", label: "Any exit load" },
  { id: "0.05", label: "0.05%" },
  { id: "0.1", label: "0.10%" },
  { id: "0.2", label: "0.20%" },
] as const;

function Home() {
  const saved = Route.useLoaderData();
  const savedSet = useMemo(() => new Set(saved.map((s) => s.slug)), [saved]);
  const [q, setQ] = useState("");
  const [risk, setRisk] = useState<(typeof RISKS)[number]>("All");
  const [type, setType] = useState<(typeof TYPES)[number]>("All");
  const [option, setOption] = useState<(typeof OPTIONS)[number]>("All");
  const [exit, setExit] = useState<(typeof EXITS)[number]["id"]>("All");

  const list = useMemo(() => {
    const query = q.trim().toLowerCase();
    return (funds as Fund[]).filter((f) => {
      if (risk !== "All" && f.risk !== risk) return false;
      if (type !== "All" && f.type !== type) return false;
      if (option !== "All" && f.option !== option) return false;
      if (exit !== "All" && String(f.exitLoad) !== exit) return false;
      if (query && !`${f.name} ${f.category}`.toLowerCase().includes(query)) return false;
      return true;
    });
  }, [q, risk, type, option, exit]);

  return (
    <main className="mx-auto max-w-3xl px-4 pb-16 pt-8">
      <p className="text-sm tracking-wide text-muted uppercase">NavLab</p>
      <h1 className="font-display mt-1 text-4xl text-fg">Funds</h1>
      <p className="mt-2 max-w-xl text-muted">
        {funds.length} schemes from your list. Open one, enter its Rupeevest scheme code once, and the holdings stay saved.
      </p>

      <label className="mt-6 flex items-center gap-2 rounded-card border border-line bg-surface px-3 py-3">
        <Search className="size-4 text-muted" aria-hidden />
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search by name or category"
          className="w-full bg-transparent text-fg outline-none placeholder:text-muted"
        />
      </label>

      <FilterRow label="Payout">
        {OPTIONS.map((o) => (
          <Chip key={o} on={option === o} onClick={() => setOption(o)}>{o}</Chip>
        ))}
      </FilterRow>
      <FilterRow label="Type">
        {TYPES.map((o) => (
          <Chip key={o} on={type === o} onClick={() => setType(o)}>{o}</Chip>
        ))}
      </FilterRow>
      <FilterRow label="Risk">
        {RISKS.map((o) => (
          <Chip key={o} on={risk === o} onClick={() => setRisk(o)}>{o}</Chip>
        ))}
      </FilterRow>
      <FilterRow label="Exit load">
        {EXITS.map((o) => (
          <Chip key={o.id} on={exit === o.id} onClick={() => setExit(o.id)}>{o.label}</Chip>
        ))}
      </FilterRow>

      <p className="mt-6 text-sm text-muted">{list.length} shown · {savedSet.size} with holdings saved</p>
      <ul className="mt-3 flex flex-col gap-2">
        {list.map((f) => (
          <li key={f.slug}>
            <Link
              to="/fund/$slug"
              params={{ slug: f.slug }}
              className="block rounded-card border border-line bg-surface px-4 py-3 transition-colors hover:bg-surface-2"
            >
              <div className="flex items-start justify-between gap-3">
                <div>
                  <p className="font-medium text-fg">{f.name}</p>
                  <p className="text-sm text-muted">{f.category}</p>
                </div>
                <span className="shrink-0 text-sm text-muted">AUM {formatAum(f.aum)}</span>
              </div>
              <div className="mt-2 flex flex-wrap gap-2">
                <Tag>{f.option}</Tag>
                <Tag>{f.type}</Tag>
                <Tag>{f.risk}</Tag>
                <Tag>Exit {f.exitLoad}%</Tag>
                <Tag>TER {f.expense}%</Tag>
                {savedSet.has(f.slug) ? <Tag strong>Holdings saved</Tag> : null}
              </div>
            </Link>
          </li>
        ))}
      </ul>
    </main>
  );
}

function FilterRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="mt-4">
      <p className="mb-2 text-xs tracking-wide text-muted uppercase">{label}</p>
      <div className="flex flex-wrap gap-2">{children}</div>
    </div>
  );
}

function Chip({ on, onClick, children }: { on: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`min-h-11 rounded-full px-3 text-sm ${on ? "bg-accent text-accent-ink" : "border border-line bg-surface text-fg"}`}
    >
      {children}
    </button>
  );
}

function Tag({ children, strong }: { children: ReactNode; strong?: boolean }) {
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs ${strong ? "bg-accent text-accent-ink" : "bg-surface-2 text-muted"}`}>
      {children}
    </span>
  );
}

function formatAum(n: number) {
  return `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 0 })} Cr`;
}

