'use client'

import { FormEvent, useCallback, useEffect, useState } from 'react'

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'

type ResearchConfig = {
  default_snapshot: { id: string; version: number } | null
  observation_start: string | null
  observation_end: string | null
  evaluation_end: string | null
  lookback_days: number
  skip_days: number
  factor_coverage: number
  label_coverage: number
  min_valid_securities: number
}

type ResearchRun = {
  id: string
  experiment_id: string
  status: string
  current_date?: string | null
  processed_dates: number
  total_dates: number
  warnings: { code: string; detail?: string }[]
  summary?: Record<string, number | string | null>
  error_summary?: string | null
}

type Bundle = {
  id: string
  data_snapshot_id: string
  status: string
  pyqlib_version: string
  size_bytes: number
  deletable: boolean
  error_summary?: string | null
}

type ResearchResults = {
  summary: Record<string, number | string | null>
  daily_metrics: Record<string, unknown>[]
  weekly_metrics: Record<string, unknown>[]
}

type RankedScores = {
  observation_date: string | null
  scores: { instrument_id: string; raw_score: number | null; average_rank: number | null; rank_percentile: number | null; factor_reason?: string | null }[]
  exclusions: { instrument_id: string; reason: string }[]
}

const ACTIVE = new Set([
  'queued', 'waiting_for_bundle', 'computing_factors', 'computing_labels', 'evaluating', 'publishing',
])

const PHASES: Record<string, string> = {
  queued: '等待 worker',
  waiting_for_bundle: '准备 Qlib 数据包',
  computing_factors: '计算动量与股票域',
  computing_labels: '计算未来收益标签',
  evaluating: '计算 IC 与分组收益',
  publishing: '发布研究产物',
  succeeded: '完成',
  failed: '失败',
  cancelled: '已取消',
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}/api/v1${path}`, init)
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(body.detail ?? `请求失败（${response.status}）`)
  }
  return response.json()
}

export default function ResearchCenter() {
  const [config, setConfig] = useState<ResearchConfig | null>(null)
  const [runs, setRuns] = useState<ResearchRun[]>([])
  const [bundles, setBundles] = useState<Bundle[]>([])
  const [active, setActive] = useState<ResearchRun | null>(null)
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [lookback, setLookback] = useState(126)
  const [skip, setSkip] = useState(21)
  const [error, setError] = useState<string | null>(null)
  const [results, setResults] = useState<ResearchResults | null>(null)
  const [ranked, setRanked] = useState<RankedScores | null>(null)

  const refresh = useCallback(async () => {
    try {
      const [settings, history, dataBundles] = await Promise.all([
        api<ResearchConfig>('/research/config'),
        api<ResearchRun[]>('/research/runs'),
        api<Bundle[]>('/qlib-data-bundles'),
      ])
      setConfig(settings)
      setRuns(history)
      setBundles(dataBundles)
      setActive(history.find((run) => ACTIVE.has(run.status)) ?? null)
      setStart((value) => value || settings.observation_start || '')
      setEnd((value) => value || settings.observation_end || '')
      setLookback((value) => value || settings.lookback_days)
      setSkip((value) => value || settings.skip_days)
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '无法加载研究状态')
    }
  }, [])

  useEffect(() => {
    const id = window.setTimeout(() => void refresh(), 0)
    return () => window.clearTimeout(id)
  }, [refresh])

  useEffect(() => {
    if (!active) return
    const id = window.setInterval(() => void refresh(), 3000)
    return () => window.clearInterval(id)
  }, [active, refresh])

  async function createRun(event: FormEvent) {
    event.preventDefault()
    if (!config?.default_snapshot) return
    try {
      const run = await api<ResearchRun>('/research/runs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          data_snapshot_id: config.default_snapshot.id,
          observation_start: start,
          observation_end: end,
          lookback_days: lookback,
          skip_days: skip,
        }),
      })
      setActive(ACTIVE.has(run.status) ? run : null)
      setRuns((current) => [run, ...current.filter((item) => item.id !== run.id)])
      setResults(null)
      setRanked(null)
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '无法创建研究运行')
    }
  }

  async function cancelRun() {
    if (!active) return
    try {
      const run = await api<ResearchRun>(`/research/runs/${active.id}/cancel`, { method: 'POST' })
      setActive(ACTIVE.has(run.status) ? run : null)
      setRuns((current) => [run, ...current.filter((item) => item.id !== run.id)])
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '无法取消研究运行')
    }
  }

  async function prebuildBundle() {
    if (!config?.default_snapshot) return
    try {
      await api<Bundle>('/qlib-data-bundles', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ data_snapshot_id: config.default_snapshot.id }),
      })
      await refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '无法预构建 Qlib 数据包')
    }
  }

  async function deleteBundle(bundle: Bundle) {
    try {
      await api<Bundle>(`/qlib-data-bundles/${bundle.id}`, { method: 'DELETE' })
      await refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '无法删除 Qlib 数据包')
    }
  }

  const latest = active ?? runs[0] ?? null

  useEffect(() => {
    if (!latest || latest.status !== 'succeeded') return
    let cancelled = false
    void api<ResearchResults>(`/research/runs/${latest.id}/results`).then((value) => {
      if (cancelled) return
      setResults(value)
      const date = value.summary.effective_factor_end
      if (typeof date === 'string') {
        void api<RankedScores>(`/research/runs/${latest.id}/ranked-scores?observation_date=${date}`).then((scores) => {
          if (!cancelled) setRanked(scores)
        })
      }
    }).catch((cause) => {
      if (!cancelled) setError(cause instanceof Error ? cause.message : '无法加载研究结果')
    })
    return () => { cancelled = true }
  }, [latest])

  return (
    <section className="research-center" aria-labelledby="research-center-title">
      <div className="section-heading">
        <div>
          <p className="eyebrow">QLIB RESEARCH</p>
          <h2 id="research-center-title">Qlib 动量因子研究</h2>
        </div>
        <span className="status-pill">pyqlib 0.9.7</span>
      </div>

      <p className="notice">
        06 发布因子评价与 RankedScores，不生成目标组合、ResearchBacktest 或模拟账务。
      </p>
      {error && <p className="error" role="alert">{error}</p>}

      <form className="research-form" onSubmit={(event) => void createRun(event)}>
        <label>数据快照<input value={config?.default_snapshot ? `v${config.default_snapshot.version}` : ''} readOnly /></label>
        <label>观察起点<input type="date" value={start} onChange={(event) => setStart(event.target.value)} /></label>
        <label>观察终点<input type="date" value={end} onChange={(event) => setEnd(event.target.value)} /></label>
        <label>回看交易日<input type="number" value={lookback} min={1} max={504} onChange={(event) => setLookback(Number(event.target.value))} /></label>
        <label>跳过交易日<input type="number" value={skip} min={1} max={126} onChange={(event) => setSkip(Number(event.target.value))} /></label>
        <button className="primary" disabled={!config?.default_snapshot || Boolean(active)}>创建研究运行</button>
      </form>

      <div className="research-rules">
        <p>日度标签：下一开盘至第六个后续开盘（5 个交易时段）</p>
        <p>周度标签：本周下一实际开盘至下周下一实际开盘</p>
        <p>因子与标签覆盖率均需达到 90%，且至少 {config?.min_valid_securities ?? 100} 只证券</p>
        <p>默认有效评价终点：{config?.evaluation_end ?? '等待快照'}</p>
      </div>

      {latest && (
        <article className="run-card research-run">
          <div className="run-title"><strong>{latest.status}</strong><span>{PHASES[latest.status] ?? latest.status}</span></div>
          <div className="progress-track" aria-label="研究日期处理进度">
            <span style={{ width: latest.total_dates ? `${Math.min(100, latest.processed_dates / latest.total_dates * 100)}%` : '0%' }} />
          </div>
          <p className="endpoint">{latest.processed_dates} / {latest.total_dates} · {latest.current_date ?? '尚未开始计算'}</p>
          {active && <button className="secondary" onClick={() => void cancelRun()}>取消研究运行</button>}
          {latest.error_summary && <p className="error">{latest.error_summary}</p>}
          {latest.warnings?.map((warning) => <p className="warning" key={warning.code}>{warning.code}</p>)}
          {latest.summary && (
            <dl className="metrics">
              <div><dt>周度 IC</dt><dd>{latest.summary.weekly_ic_mean ?? 'unavailable'}</dd></div>
              <div><dt>周度 ICIR</dt><dd>{latest.summary.weekly_icir ?? 'unavailable'}</dd></div>
              <div><dt>周度 Rank IC</dt><dd>{latest.summary.weekly_rank_ic_mean ?? 'unavailable'}</dd></div>
              <div><dt>周度 Rank ICIR</dt><dd>{latest.summary.weekly_rank_icir ?? 'unavailable'}</dd></div>
            </dl>
          )}
        </article>
      )}

      {latest?.status === 'succeeded' && results && (
        <>
          <MetricTable title="周度黄金口径" rows={results.weekly_metrics} />
          <MetricTable title="日度诊断" rows={results.daily_metrics} />
        </>
      )}

      {latest?.status === 'succeeded' && ranked && (
        <div className="ranked-scores">
          <h3>RankedScores · {ranked.observation_date}</h3>
          <table className="findings">
            <thead><tr><th>证券身份</th><th>原始动量</th><th>平均秩</th><th>百分位</th><th>状态</th></tr></thead>
            <tbody>{ranked.scores.slice(0, 100).map((row) => (
              <tr key={row.instrument_id}>
                <td>{row.instrument_id}</td>
                <td>{row.raw_score == null ? 'unavailable' : row.raw_score.toFixed(6)}</td>
                <td>{row.average_rank ?? '—'}</td>
                <td>{row.rank_percentile == null ? '—' : row.rank_percentile.toLocaleString('zh-CN', { style: 'percent', maximumFractionDigits: 1 })}</td>
                <td>{row.factor_reason ?? '有效'}</td>
              </tr>
            ))}</tbody>
          </table>
          <h4>股票域排除原因</h4>
          {ranked.exclusions.length === 0 ? <p className="empty">该截面没有股票域排除项。</p> : (
            <table className="findings">
              <thead><tr><th>证券身份</th><th>原因</th></tr></thead>
              <tbody>{ranked.exclusions.slice(0, 100).map((row) => (
                <tr key={`${row.instrument_id}-${row.reason}`}><td>{row.instrument_id}</td><td>{row.reason}</td></tr>
              ))}</tbody>
            </table>
          )}
        </div>
      )}

      <div className="bundle-list">
        <div className="run-title">
          <h3>Qlib 数据包</h3>
          <button className="secondary" disabled={!config?.default_snapshot} onClick={() => void prebuildBundle()}>预构建当前快照</button>
        </div>
        {bundles.length === 0 ? <p className="empty">运行研究时将按需构建数据包。</p> : bundles.map((bundle) => (
          <div className="history-row" key={bundle.id}>
            <span>{bundle.id.slice(0, 8)}</span><strong>{bundle.status}</strong>
            <span>{(bundle.size_bytes / 1024 / 1024).toFixed(1)} MB · pyqlib {bundle.pyqlib_version}</span>
            <button className="secondary" disabled={!bundle.deletable} onClick={() => void deleteBundle(bundle)}>删除数据包</button>
          </div>
        ))}
      </div>
    </section>
  )
}

function MetricTable({ title, rows }: { title: string; rows: Record<string, unknown>[] }) {
  return (
    <div className="research-results">
      <h3>{title}</h3>
      <table className="findings">
        <thead><tr><th>截面日</th><th>IC</th><th>Rank IC</th><th>因子覆盖</th><th>标签覆盖</th><th>最高减最低组</th></tr></thead>
        <tbody>{rows.map((row, index) => (
          <tr key={`${String(row.observation_date)}-${index}`}>
            <td>{String(row.observation_date ?? '—').slice(0, 10)}</td>
            <td>{row.ic == null ? 'unavailable' : Number(row.ic).toFixed(4)}</td>
            <td>{row.rank_ic == null ? 'unavailable' : Number(row.rank_ic).toFixed(4)}</td>
            <td>{Number(row.factor_coverage ?? 0).toLocaleString('zh-CN', { style: 'percent', maximumFractionDigits: 1 })}</td>
            <td>{Number(row.label_coverage ?? 0).toLocaleString('zh-CN', { style: 'percent', maximumFractionDigits: 1 })}</td>
            <td>{row.long_short_return == null ? 'unavailable' : Number(row.long_short_return).toFixed(4)}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  )
}
