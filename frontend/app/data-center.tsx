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
  task_attempt?: number
  error_code?: string | null
  error_summary?: string | null
  created_at?: string
}

const TERMINAL_FAILURES = new Set(['failed', 'partial_failed', 'cancelled'])

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
  evaluating_quality: '校验数据质量',
  activating_snapshot: '激活数据快照',
  complete: '已完成',
}

/**
 * Snapshots are produced by a successful sync, never created by hand, so this
 * is a read-only view of what already exists.
 */
type Snapshot = {
  id: string
  version: number
  coverage_start?: string | null
  coverage_end?: string | null
  verified_start?: string | null
  verified_end?: string | null
  is_backtest_eligible: boolean
  is_current: boolean
  created_at?: string
}

type Finding = {
  rule: string
  trade_date: string
  severity: 'warning' | 'rejecting'
  affected_count: number
  evaluated_count: number
  sample: string[]
}

type SnapshotDetail = Snapshot & { findings: Finding[] }

const RULE_LABELS: Record<string, string> = {
  missing_critical_field: '关键字段缺失',
  missing_optional_field: '非关键字段缺失',
  negative_price: '价格为负',
  negative_volume: '成交量为负',
  ohlc_out_of_order: 'OHLC 逻辑错误',
  calendar_disagreement: '日历修订后仍有行情',
  missing_trading_day: '开市日缺 K 线',
  no_trading_activity: '停牌/零成交',
  adjustment_inconsistent: '复权比值无故突变',
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
  const [snapshots, setSnapshots] = useState<Snapshot[]>([])
  const [detail, setDetail] = useState<SnapshotDetail | null>(null)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const status = await jsonFetch<SourceStatus>('/data-sync/status')
      const history = await jsonFetch<SyncRun[]>('/data-sync/runs')
      const stored = await jsonFetch<Snapshot[]>('/snapshots')
      setSource(status)
      setRuns(history)
      setSnapshots(stored)
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

  async function inspect(snapshotId: string) {
    if (detail?.id === snapshotId) {
      setDetail(null)
      return
    }
    try {
      setDetail(await jsonFetch<SnapshotDetail>(`/snapshots/${snapshotId}`))
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '无法加载快照详情')
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

          {/* Tucked away on purpose: this overturns a verdict the system
              already reached, so it should not sit beside 立即同步. No
              confirmation dialog — showing the reason and the attempt count
              before the click is worth more than asking afterwards. */}
          {TERMINAL_FAILURES.has(latest.status) && (
            <details className="escalate">
              <summary>更多</summary>
              <p className="endpoint">
                失败原因：{latest.error_summary ?? latest.error_code ?? '未记录'}
                {' · '}已尝试 {latest.task_attempt ?? 0} 次
              </p>
              <p className="notice">
                强制重试会清零尝试次数并让这次运行从检查点继续。已发布的批次不会重抓。
              </p>
              <button
                className="secondary"
                onClick={() => void command(`/data-sync/runs/${latest.id}/force-retry`, '无法强制重试')}
              >强制重试</button>
            </details>
          )}
        </article>
      ) : <p className="empty">尚无同步运行。配置密钥后可开始首次回填。</p>}

      {snapshots.length > 0 && <div className="snapshots">
        <h3>数据快照</h3>
        {snapshots.slice(0, 8).map((snapshot) => (
          <div key={snapshot.id}>
            <button
              type="button"
              className="snapshot-row"
              aria-expanded={detail?.id === snapshot.id}
              onClick={() => void inspect(snapshot.id)}
            >
              <strong>v{snapshot.version}</strong>
              <span>{snapshot.coverage_start ?? '—'} → {snapshot.coverage_end ?? '—'}</span>
              <span className={snapshot.is_backtest_eligible ? 'eligible' : 'ineligible'}>
                {snapshot.is_backtest_eligible ? '可用于回测' : '不可用于回测'}
              </span>
              {snapshot.is_current && <span className="current">当前</span>}
            </button>
            {detail?.id === snapshot.id && (
              <div className="snapshot-detail">
                <p className="endpoint">
                  本次核对范围 {detail.verified_start ?? '—'} → {detail.verified_end ?? '—'}
                </p>
                {detail.findings.length === 0 ? (
                  <p className="empty">质量检查未发现问题。</p>
                ) : (
                  <table className="findings">
                    <thead>
                      <tr><th>规则</th><th>交易日</th><th>命中</th><th>样本</th></tr>
                    </thead>
                    <tbody>
                      {detail.findings.map((finding) => (
                        <tr
                          key={`${finding.rule}-${finding.trade_date}`}
                          className={finding.severity === 'rejecting' ? 'rejecting' : 'warning'}
                        >
                          <td>{RULE_LABELS[finding.rule] ?? finding.rule}</td>
                          <td>{finding.trade_date}</td>
                          <td>{finding.affected_count} / {finding.evaluated_count}</td>
                          <td>{finding.sample.slice(0, 5).join('、') || '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            )}
          </div>
        ))}
      </div>}

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
