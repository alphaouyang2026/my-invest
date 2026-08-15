'use client'

import { useCallback, useEffect, useState } from 'react'

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'

/**
 * `sync now` takes no parameters — batch size and date ranges are server-side
 * policy, not user input. Progress is reported per batch because a first
 * import can run for hours across many publications.
 */
type SyncRun = {
  id: string
  status: string
  phase?: string | null
  mode?: string | null
  target_dates?: number
  processed_dates?: number
  total_batches?: number
  completed_batches?: number
  current_batch?: number | null
  current_date?: string | null
  rows_received?: number
  rows_new?: number
  rows_unchanged?: number
  rows_changed?: number
  actual_min?: string | null
  actual_max?: string | null
  resumable?: boolean
  error_summary?: string | null
  created_at?: string
}

type SourceStatus = {
  configuration: 'not_configured' | 'configured' | 'invalid'
  plan_notice: string
  latest_run: SyncRun | null
}

const terminal = new Set(['succeeded', 'no_change', 'partial_failed', 'failed', 'cancelled'])

const PHASE_LABELS: Record<string, string> = {
  discovering_calendar: '获取交易日历',
  planning: '冻结同步计划',
  bars: '同步日线',
  master: '同步证券主数据',
  activating_snapshot: '激活数据快照',
  complete: '已完成',
}

async function jsonFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}/api/v1${path}`, init)
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}))
    throw new Error(payload.detail ?? `请求失败（${response.status}）`)
  }
  return response.json()
}

export default function DataCenter() {
  const [source, setSource] = useState<SourceStatus | null>(null)
  const [runs, setRuns] = useState<SyncRun[]>([])
  const [active, setActive] = useState<SyncRun | null>(null)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const status = await jsonFetch<SourceStatus>('/data-sync/status')
      const history = await jsonFetch<SyncRun[]>('/data-sync/runs')
      setSource(status)
      setRuns(history)
      setActive(history.find((run) => !terminal.has(run.status)) ?? null)
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '无法加载数据源状态')
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

  async function command(path: string, failure: string) {
    try {
      const run = await jsonFetch<SyncRun>(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      })
      setActive(terminal.has(run.status) ? null : run)
      setRuns((current) => [run, ...current.filter((item) => item.id !== run.id)])
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : failure)
    }
  }

  const latest = active ?? source?.latest_run ?? runs[0] ?? null
  const resumable = !active && latest?.resumable === true

  return (
    <section className="data-center" aria-labelledby="data-center-title">
      <div className="section-heading">
        <div>
          <p className="eyebrow">DATA SOURCE</p>
          <h2 id="data-center-title">J-Quants 数据中心</h2>
        </div>
        <span className={`status-pill status-${source?.configuration ?? 'loading'}`}>
          {source?.configuration ?? 'loading'}
        </span>
      </div>

      <p className="notice">{source?.plan_notice ?? '正在读取数据源状态…'}</p>
      {error && <p className="error" role="alert">{error}</p>}

      <div className="actions">
        <button
          className="primary"
          disabled={source?.configuration !== 'configured' || Boolean(active)}
          onClick={() => void command('/data-sync/jquants', '无法启动同步')}
        >立即同步</button>
        {active && (
          <button
            className="secondary"
            onClick={() => void command(`/data-sync/runs/${active.id}/cancel`, '无法取消同步')}
          >取消任务</button>
        )}
        {resumable && latest && (
          <button
            className="secondary"
            onClick={() => void command(`/data-sync/runs/${latest.id}/resume`, '无法恢复同步')}
          >恢复运行</button>
        )}
      </div>

      {latest ? (
        <article className="run-card">
          <div className="run-title">
            <strong>{latest.status}</strong>
            <span>{latest.mode ?? '等待 worker 接手'}</span>
          </div>
          <div className="progress-track" aria-label="日期处理进度">
            <span style={{ width: latest.target_dates ? `${Math.min(100, (latest.processed_dates ?? 0) / latest.target_dates * 100)}%` : '0%' }} />
          </div>
          <dl className="metrics">
            <div><dt>日期</dt><dd>{latest.processed_dates ?? 0} / {latest.target_dates ?? 0}</dd></div>
            <div><dt>批次</dt><dd>{latest.completed_batches ?? 0} / {latest.total_batches ?? 0}</dd></div>
            <div><dt>新增</dt><dd>{latest.rows_new ?? 0}</dd></div>
            <div><dt>修订</dt><dd>{latest.rows_changed ?? 0}</dd></div>
          </dl>
          <p className="endpoint">
            {PHASE_LABELS[latest.phase ?? ''] ?? latest.phase ?? '—'}
            {latest.current_date ? ` · 正在取 ${latest.current_date}` : ''}
            {' · '}{latest.actual_min ?? '—'} → {latest.actual_max ?? '—'}
          </p>
          {latest.error_summary && <p className="error">{latest.error_summary}</p>}
        </article>
      ) : <p className="empty">尚无同步运行。配置密钥后可开始首次回填。</p>}

      {runs.length > 0 && <div className="history">
        <h3>最近运行</h3>
        {runs.slice(0, 8).map((run) => (
          <div className="history-row" key={run.id}>
            <span>{run.created_at ? new Date(run.created_at).toLocaleString('zh-CN') : run.id.slice(0, 8)}</span>
            <strong>{run.status}</strong>
            <span>{run.actual_min ?? '—'} → {run.actual_max ?? '—'}</span>
          </div>
        ))}
      </div>}
    </section>
  )
}
