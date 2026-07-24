import { useState } from 'react'
import {
  Activity,
  CheckCircle2,
  CircleDashed,
  DollarSign,
  FileText,
  Layers,
  Play,
  Timer,
  XCircle,
} from 'lucide-react'
import { ErrorState } from '../components/States'
import { useApi, usePersistedState } from '../hooks/useApi'
import { api } from '../lib/api'
import { formatCurrency, formatNumber, relativeTime } from '../lib/format'

function StatTile({ icon: Icon, label, value, hint, tone = 'default' }) {
  const tones = {
    default: 'text-ink',
    good: 'text-emerald-600 dark:text-emerald-400',
    warn: 'text-amber-600 dark:text-amber-400',
    bad: 'text-red-600 dark:text-red-400',
  }
  return (
    <div className="card p-5">
      <div className="flex items-center gap-2 text-faint">
        <Icon size={13} />
        <span className="label">{label}</span>
      </div>
      <p className={`mt-3 text-2xl font-bold tabular-nums tracking-tight ${tones[tone]}`}>
        {value}
      </p>
      {hint && <p className="mt-1 text-xs text-muted">{hint}</p>}
    </div>
  )
}

function StatusBadge({ status }) {
  const map = {
    succeeded: ['bg-emerald-500/12 text-emerald-600 dark:text-emerald-400', CheckCircle2],
    partial: ['bg-amber-500/12 text-amber-600 dark:text-amber-400', CircleDashed],
    failed: ['bg-red-500/12 text-red-600 dark:text-red-400', XCircle],
    running: ['bg-blue-500/12 text-blue-600 dark:text-blue-400', Activity],
  }
  const [cls, Icon] = map[status] || ['bg-raised text-muted', CircleDashed]
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1
                  font-mono text-2xs uppercase tracking-wider ${cls}`}
    >
      <Icon size={11} />
      {status}
    </span>
  )
}

/** Trigger control. The API key is entered once and kept in localStorage —
 *  this is an operator tool, not a public page. */
function RunTrigger({ onDone }) {
  const [apiKey, setApiKey] = usePersistedState('adminKey', '')
  const [state, setState] = useState({ busy: false, message: null, error: false })

  const trigger = async () => {
    if (!apiKey) {
      setState({ busy: false, message: 'Enter the admin API key first.', error: true })
      return
    }
    setState({ busy: true, message: null, error: false })
    try {
      const res = await api.triggerRun(apiKey)
      setState({ busy: false, message: `Queued — task ${res.task_id.slice(0, 8)}…`, error: false })
      setTimeout(onDone, 2500)
    } catch (err) {
      setState({
        busy: false,
        error: true,
        message: err.status === 401 ? 'Invalid API key.' : err.message,
      })
    }
  }

  return (
    <div className="card space-y-3 p-5">
      <p className="label">Trigger a run</p>
      <p className="text-xs leading-relaxed text-muted">
        Needs the always-on worker. On a free deployment the pipeline runs in
        GitHub Actions instead — open the repo's{' '}
        <span className="font-medium text-ink">Actions → Publish</span> tab and
        run the workflow (tick “dry run” first).
      </p>
      <div className="flex flex-col gap-2 sm:flex-row">
        <input
          type="password"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder="ADMIN_API_KEY"
          aria-label="Admin API key"
          className="min-w-0 flex-1 rounded-xl border border-line bg-canvas px-4 py-2.5
                     font-mono text-sm text-ink placeholder:text-faint
                     transition-colors focus:border-accent"
        />
        <button
          type="button"
          onClick={trigger}
          disabled={state.busy}
          className="btn-primary shrink-0"
        >
          <Play size={14} />
          {state.busy ? 'Queueing…' : 'Run pipeline'}
        </button>
      </div>
      {state.message && (
        <p className={`text-xs ${state.error ? 'text-red-500' : 'text-emerald-500'}`}>
          {state.message}
        </p>
      )}
    </div>
  )
}

export default function Dashboard() {
  const stats = useApi((o) => api.pipelineStats({ days: 30 }, o), [])
  const costs = useApi((o) => api.costs({ days: 30 }, o), [])
  const runs = useApi((o) => api.listRuns({ limit: 12 }, o), [])

  const error = stats.error || costs.error || runs.error
  if (error) {
    return (
      <div className="shell">
        <ErrorState
          error={error}
          onRetry={() => {
            stats.refetch()
            costs.refetch()
            runs.refetch()
          }}
        />
      </div>
    )
  }

  const s = stats.data
  const c = costs.data
  const loading = stats.loading || costs.loading

  const successTone =
    !s ? 'default' : s.success_rate >= 0.9 ? 'good' : s.success_rate >= 0.7 ? 'warn' : 'bad'

  return (
    <div className="shell space-y-8 py-14">
      <header className="space-y-2">
        <span className="label">Last 30 days</span>
        <h1 className="text-3xl font-extrabold tracking-tight sm:text-4xl">
          Pipeline
        </h1>
        <p className="max-w-lg text-muted">
          Throughput, reliability and spend for the automated publishing pipeline.
        </p>
      </header>

      {loading ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {[...Array(8)].map((_, i) => (
            <div key={i} className="skeleton h-[7.5rem] rounded-2xl" />
          ))}
        </div>
      ) : (
        <>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <StatTile
              icon={FileText}
              label="Published"
              value={formatNumber(s?.posts_published)}
              hint="articles live"
            />
            <StatTile
              icon={CheckCircle2}
              label="Success rate"
              value={s ? `${(s.success_rate * 100).toFixed(0)}%` : '—'}
              hint={`${s?.runs_succeeded ?? 0} of ${s?.runs_total ?? 0} runs`}
              tone={successTone}
            />
            <StatTile
              icon={Timer}
              label="Avg duration"
              value={s ? `${Math.round(s.avg_duration_seconds / 60)}m` : '—'}
              hint="per run, end to end"
            />
            <StatTile
              icon={DollarSign}
              label="Cost / post"
              value={formatCurrency(c?.cost_per_post_usd)}
              hint={`${formatCurrency(c?.total_cost_usd)} total`}
            />
            <StatTile
              icon={Layers}
              label="Articles collected"
              value={formatNumber(s?.articles_collected)}
              hint="raw items ingested"
            />
            <StatTile
              icon={CircleDashed}
              label="Dedupe compression"
              value={s ? `${(s.dedupe_compression * 100).toFixed(0)}%` : '—'}
              hint="duplicates collapsed"
            />
            <StatTile
              icon={Activity}
              label="Projected monthly"
              value={formatCurrency(c?.projected_monthly_usd)}
              hint={`budget ${formatCurrency(c?.budget_limit_usd)}`}
              tone={
                c && c.projected_monthly_usd > c.budget_limit_usd * 0.8 ? 'warn' : 'good'
              }
            />
            <StatTile
              icon={XCircle}
              label="Failed runs"
              value={formatNumber(s?.runs_failed)}
              hint="last 30 days"
              tone={s?.runs_failed > 0 ? 'warn' : 'good'}
            />
          </div>

          <RunTrigger onDone={runs.refetch} />

          {/* ---------------------------------------------- cost breakdown */}
          {c?.breakdown?.length > 0 && (
            <section className="space-y-4">
              <h2 className="text-lg font-semibold tracking-tight">Spend by model</h2>
              <div className="card divide-y divide-line overflow-hidden">
                {c.breakdown.slice(0, 6).map((row, i) => {
                  const pct = c.total_cost_usd
                    ? (row.cost_usd / c.total_cost_usd) * 100
                    : 0
                  return (
                    <div key={i} className="flex items-center gap-4 p-4">
                      <div className="min-w-0 flex-1">
                        <p className="truncate font-mono text-xs text-ink">{row.model}</p>
                        <p className="mt-0.5 text-2xs text-faint">
                          {row.category} · {formatNumber(row.requests)} calls ·{' '}
                          {formatNumber(row.input_tokens)} in
                        </p>
                      </div>
                      <div className="hidden h-1.5 w-32 overflow-hidden rounded-full bg-raised sm:block">
                        <div
                          className="h-full rounded-full bg-accent"
                          style={{ width: `${pct}%` }}
                        />
                      </div>
                      <span className="w-20 text-right font-mono text-sm tabular-nums">
                        {formatCurrency(row.cost_usd)}
                      </span>
                    </div>
                  )
                })}
              </div>
            </section>
          )}

          {/* ------------------------------------------------- recent runs */}
          <section className="space-y-4">
            <h2 className="text-lg font-semibold tracking-tight">Recent runs</h2>
            {runs.data?.length ? (
              <div className="card overflow-x-auto">
                <table className="w-full min-w-[42rem] text-sm">
                  <thead>
                    <tr className="border-b border-line">
                      {['When', 'Trigger', 'Status', 'Collected', 'Unique', 'Posts', 'Cost'].map(
                        (h) => (
                          <th
                            key={h}
                            className="px-4 py-3 text-left font-mono text-2xs uppercase
                                       tracking-wider text-faint"
                          >
                            {h}
                          </th>
                        ),
                      )}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-line/60">
                    {runs.data.map((run) => (
                      <tr key={run.id} className="transition-colors hover:bg-raised/50">
                        <td className="whitespace-nowrap px-4 py-3 text-muted">
                          {relativeTime(run.created_at)}
                        </td>
                        <td className="px-4 py-3">
                          <span className="font-mono text-xs text-muted">{run.trigger}</span>
                        </td>
                        <td className="px-4 py-3">
                          <StatusBadge status={run.status} />
                        </td>
                        <td className="px-4 py-3 tabular-nums text-muted">
                          {formatNumber(run.articles_collected)}
                        </td>
                        <td className="px-4 py-3 tabular-nums text-muted">
                          {formatNumber(run.articles_after_dedupe)}
                        </td>
                        <td className="px-4 py-3 tabular-nums font-medium">
                          {run.posts_created}
                        </td>
                        <td className="px-4 py-3 text-right font-mono tabular-nums text-muted">
                          {formatCurrency(Number(run.total_cost_usd))}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="card p-8 text-center text-sm text-muted">
                No runs yet. Trigger one above, or wait for the next scheduled slot.
              </p>
            )}
          </section>
        </>
      )}
    </div>
  )
}
