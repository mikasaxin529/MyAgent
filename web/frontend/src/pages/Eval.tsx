import { useState } from "react";
import { postEval, type EvalResult } from "../api";

export default function EvalPage() {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<EvalResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run() {
    setLoading(true);
    setError(null);
    try {
      setResult(await postEval());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="h-full flex flex-col">
      <header className="px-6 py-4 border-b border-slate-800 flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">Eval</h1>
          <p className="text-xs text-slate-500">Run the evaluation harness and inspect per-tag results.</p>
        </div>
        <button
          onClick={run}
          disabled={loading}
          className="px-5 py-2 rounded-md bg-indigo-600 hover:bg-indigo-500 disabled:bg-slate-800 disabled:text-slate-600 text-white text-sm font-medium"
        >
          {loading ? "Running…" : "Run eval"}
        </button>
      </header>

      <div className="flex-1 overflow-auto px-6 py-6 space-y-6">
        {error && (
          <div className="px-3 py-2 rounded-md bg-rose-500/10 border border-rose-500/30 text-rose-300 text-sm">
            {error}
          </div>
        )}
        {!result && !error && (
          <div className="h-full flex items-center justify-center text-slate-600 text-sm">
            No evaluation results yet.
          </div>
        )}
        {result && (
          <>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <Metric label="Accuracy" value={`${(result.accuracy * 100).toFixed(1)}%`} />
              <Metric label="Task Completion" value={`${(result.task_completion_rate * 100).toFixed(1)}%`} />
              <Metric label="Avg Latency" value={`${result.avg_latency_ms} ms`} />
              <Metric label="Avg Token Cost" value={`${result.avg_token_cost}`} />
            </div>
            <div>
              <h2 className="text-sm font-semibold text-slate-300 mb-3">Per-tag accuracy</h2>
              <PerTagBarChart result={result} />
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="px-4 py-3 rounded-lg bg-slate-900 border border-slate-800">
      <div className="text-[10px] uppercase tracking-wider text-slate-500">{label}</div>
      <div className="text-lg font-semibold text-slate-100 mt-1">{value}</div>
    </div>
  );
}

function PerTagBarChart({ result }: { result: EvalResult }) {
  const entries = Object.entries(result.per_tag);
  if (entries.length === 0) {
    return <div className="text-slate-600 text-sm">No per-tag data.</div>;
  }

  const width = 640;
  const barHeight = 22;
  const gap = 10;
  const labelW = 140;
  const valueW = 60;
  const chartW = width - labelW - valueW;
  const height = entries.length * (barHeight + gap);

  return (
    <div className="overflow-auto">
      <svg
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        className="block"
        role="img"
        aria-label="Per-tag accuracy bar chart"
      >
        {entries.map(([tag, stat], i) => {
          const y = i * (barHeight + gap);
          const pct = Math.max(0, Math.min(1, stat.accuracy));
          const barW = Math.max(2, pct * chartW);
          return (
            <g key={tag}>
              <text
                x={0}
                y={y + barHeight / 2 + 4}
                fill="#94a3b8"
                fontSize="12"
                fontFamily="ui-monospace, monospace"
              >
                {tag.length > 18 ? tag.slice(0, 17) + "…" : tag}
              </text>
              <rect
                x={labelW}
                y={y}
                width={chartW}
                height={barHeight}
                rx={4}
                fill="#1e293b"
              />
              <rect
                x={labelW}
                y={y}
                width={barW}
                height={barHeight}
                rx={4}
                fill="#6366f1"
              />
              <text
                x={labelW + chartW + 8}
                y={y + barHeight / 2 + 4}
                fill="#cbd5e1"
                fontSize="12"
                fontFamily="ui-monospace, monospace"
              >
                {(pct * 100).toFixed(0)}% · n={stat.count}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
