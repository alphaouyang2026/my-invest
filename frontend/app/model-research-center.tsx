'use client'

import { FormEvent, useCallback, useEffect, useState } from 'react'

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'

type FeatureSet = {
  name: string
  version: string
  description: string
  column_count: number
  max_window: number
  selectable: boolean
  is_default: boolean
  maturity: string
}

type ParamRange = { type: string; min: number; max: number }

type ModelConfig = {
  default_snapshot: { id: string; version: number } | null
  feature_sets: FeatureSet[]
  default_feature_set: string
  default_seed: number
  locked_params: Record<string, unknown>
  overridable_params: Record<string, ParamRange>
  splits: Record<string, string> | null
  splits_unavailable_reason?: string
}

type ModelRun = {
  id: string
  status: string
  current_date?: string | null
  processed_dates: number
  total_dates: number
  warnings: { code: string; detail?: string }[]
  summary?: Record<string, unknown>
  error_code?: string | null
  error_summary?: string | null
}

type SegmentSummary = {
  segment: string
  source: string
  observations: number
  ic_mean: number | null
  icir: number | null
  rank_ic_mean: number | null
  rank_icir: number | null
  long_short_mean: number | null
  group_monotonicity: number | null
}

type ModelResults = {
  summary: { segments: SegmentSummary[]; test_observations: number; best_iteration: number | null; zero_gain_features: number; feature_set: string }
  trained_model: Record<string, unknown> | null
  prediction_run: Record<string, unknown> | null
  feature_importance: { feature_name: string; gain: number; split: number; gain_rank: number }[]
  feature_missing_rate: { segment: string; feature_name: string; missing_rate: number }[]
  feature_group_anomalies: { segment: string; feature_group: string; source_field: string; invalid_rows: number; rows: number }[]
  training_curve: { dataset: string; metric: string; iteration: number; value: number }[]
}

type RankedScores = {
  observation_date: string | null
  scores: {
    observation_date: string
    instrument_id: string
    source_code: string | null
    raw_score: number | null
    average_rank: number | null
    rank_percentile: number | null
    normalized_score: number | null
    label_status: string
    trained_model_id: string
    data_snapshot_id: string
  }[]
}

const ACTIVE = new Set([
  'queued', 'waiting_for_bundle', 'computing_factors', 'computing_labels',
  'training', 'predicting', 'evaluating', 'publishing',
])

const PHASES: Record<string, string> = {
  queued: '等待 worker',
  waiting_for_bundle: '准备 Qlib 数据包',
  computing_factors: '计算特征与股票域',
  computing_labels: '计算周度调仓标签',
  training: '训练 LightGBM',
  predicting: '生成样本外预测',
  evaluating: '计算三段指标与动量对照',
  publishing: '发布研究产物',
  succeeded: '完成',
  failed: '失败',
  cancelled: '已取消',
}

const MODEL_SOURCE = 'lightgbm'
const MOMENTUM_SOURCE = 'momentum_6_1'

//: How much worse a column may be in test than in train before it is worth
//: showing. The full table is 158 or 360 rows and nobody reads it; the handful
//: that drifted are the ones that indicate a data problem rather than a model.
const MISSING_RATE_DRIFT = 0.1
const SCORE_PAGE_SIZE = 50

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}/api/v1${path}`, init)
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(body.detail ?? `请求失败（${response.status}）`)
  }
  return response.json()
}

function metric(value: number | null | undefined, digits = 4): string {
  return value == null ? 'unavailable' : Number(value).toFixed(digits)
}

export default function ModelResearchCenter() {
  const [config, setConfig] = useState<ModelConfig | null>(null)
  const [runs, setRuns] = useState<ModelRun[]>([])
  const [active, setActive] = useState<ModelRun | null>(null)
  const [featureSet, setFeatureSet] = useState('')
  const [splits, setSplits] = useState<Record<string, string>>({})
  const [seed, setSeed] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [results, setResults] = useState<ModelResults | null>(null)
  const [ranked, setRanked] = useState<RankedScores | null>(null)
  const [showAllFeatures, setShowAllFeatures] = useState(false)
  const [showAllMissing, setShowAllMissing] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const [settings, history] = await Promise.all([
        api<ModelConfig>('/research/model-runs/config'),
        api<ModelRun[]>('/research/runs'),
      ])
      setConfig(settings)
      setRuns(history)
      setActive(history.find((run) => ACTIVE.has(run.status)) ?? null)
      setFeatureSet((value) => value || settings.default_feature_set)
      setSeed((value) => value || settings.default_seed)
      setSplits((value) => (Object.keys(value).length ? value : settings.splits ?? {}))
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '无法加载模型研究状态')
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
      const run = await api<ModelRun>('/research/model-runs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          data_snapshot_id: config.default_snapshot.id,
          feature_set: featureSet,
          seed,
          ...splits,
        }),
      })
      setActive(ACTIVE.has(run.status) ? run : null)
      setRuns((current) => [run, ...current.filter((item) => item.id !== run.id)])
      setResults(null)
      setRanked(null)
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '无法创建模型训练运行')
    }
  }

  async function cancelRun() {
    if (!active) return
    try {
      const run = await api<ModelRun>(`/research/runs/${active.id}/cancel`, { method: 'POST' })
      setActive(ACTIVE.has(run.status) ? run : null)
      setRuns((current) => [run, ...current.filter((item) => item.id !== run.id)])
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '无法取消模型训练运行')
    }
  }

  const latest = active ?? runs.find((run) => run.summary && 'test_observations' in (run.summary ?? {})) ?? null

  useEffect(() => {
    if (!latest || latest.status !== 'succeeded') return
    let cancelled = false
    void api<ModelResults>(`/research/model-runs/${latest.id}/results`)
      .then((value) => {
        if (cancelled) return
        setResults(value)
        return api<RankedScores>(`/research/runs/${latest.id}/ranked-scores`)
      })
      .then((scores) => { if (!cancelled && scores) setRanked(scores) })
      .catch((cause) => {
        if (!cancelled) setError(cause instanceof Error ? cause.message : '无法加载模型结果')
      })
    return () => { cancelled = true }
  }, [latest])

  const selected = config?.feature_sets.find((item) => item.name === featureSet)
  const segments = results?.summary.segments ?? []
  const testModel = segments.find((s) => s.segment === 'test' && s.source === MODEL_SOURCE)
  const testMomentum = segments.find((s) => s.segment === 'test' && s.source === MOMENTUM_SOURCE)

  return (
    <section className="research-center" aria-labelledby="model-research-title">
      <div className="section-heading">
        <div>
          <p className="eyebrow">QLIB MODEL RESEARCH</p>
          <h2 id="model-research-title">多因子与 LightGBM 预测</h2>
        </div>
        <span className="status-pill">pyqlib 0.9.7</span>
      </div>

      <p className="notice">
        07 发布样本外 PredictionRun 与 RankedScores，不生成目标组合、ResearchBacktest 或模拟账务。
        分数只表示截面内的相对位次，没有收益量纲。
      </p>
      {error && <p className="error" role="alert">{error}</p>}

      <form className="research-form" onSubmit={(event) => void createRun(event)}>
        <label>数据快照<input value={config?.default_snapshot ? `v${config.default_snapshot.version}` : ''} readOnly /></label>
        <label>
          特征集
          <select value={featureSet} onChange={(event) => setFeatureSet(event.target.value)}>
            {config?.feature_sets.filter((item) => item.selectable).map((item) => (
              <option key={item.name} value={item.name}>
                {item.name}（{item.column_count} 列{item.maturity === 'experimental' ? '，实验性' : ''}）
              </option>
            ))}
          </select>
        </label>
        {(['train_start', 'train_end', 'valid_start', 'valid_end', 'test_start', 'test_end'] as const).map((key) => (
          <label key={key}>
            {key.replace('_', ' ')}
            <input
              type="date"
              value={splits[key] ?? ''}
              onChange={(event) => setSplits((current) => ({ ...current, [key]: event.target.value }))}
            />
          </label>
        ))}
        <label>随机种子<input type="number" value={seed} min={0} onChange={(event) => setSeed(Number(event.target.value))} /></label>
        <button className="primary" disabled={!config?.default_snapshot || Boolean(active)}>开始训练</button>
      </form>

      {selected?.maturity === 'experimental' && (
        <p className="warning">
          {selected.name} 为实验性特征集：{selected.column_count} 列、短历史，结果仅供探索。
        </p>
      )}
      {config?.splits_unavailable_reason && <p className="warning">{config.splits_unavailable_reason}</p>}

      <div className="research-rules">
        <p>标签：本周截面后的下一开盘至下周截面后的下一开盘（周度实际调仓）</p>
        <p>训练目标为截面秩，模型输出无收益量纲；排名以 rank_percentile 表达</p>
        <p>两段 embargo 各排除一个完整周度截面，train / valid / test 严格按交易日历隔离</p>
        <p>
          锁定参数（不可覆盖）：{Object.keys(config?.locked_params ?? {}).join('、') || '—'}
          ；num_threads 仅由部署配置决定
        </p>
      </div>

      {latest && (
        <article className="run-card research-run">
          <div className="run-title"><strong>{latest.status}</strong><span>{PHASES[latest.status] ?? latest.status}</span></div>
          <div className="progress-track" aria-label="模型研究进度">
            <span style={{ width: latest.total_dates ? `${Math.min(100, latest.processed_dates / latest.total_dates * 100)}%` : '0%' }} />
          </div>
          <p className="endpoint">{latest.processed_dates} / {latest.total_dates} · {latest.current_date ?? '尚未开始计算'}</p>
          {active && (
            <>
              <button className="secondary" onClick={() => void cancelRun()}>请求取消</button>
              {/* Cancellation is best-effort: the flag is read every 20 boosting
                  rounds, so a run that finishes first is allowed to succeed.
                  Saying "已取消" here would promise something the worker does
                  not guarantee. */}
              <p className="endpoint">取消为尽力而为：若训练在请求被观察到之前完成，运行仍会正常结束。</p>
            </>
          )}
          {latest.error_summary && <p className="error">{latest.error_code}：{latest.error_summary}</p>}
          {latest.warnings?.map((warning) => (
            <p className="warning" key={warning.code}>{warning.code}{warning.detail ? ` · ${warning.detail}` : ''}</p>
          ))}
        </article>
      )}

      {latest?.status === 'succeeded' && results && (
        <>
          <div className="research-results">
            <h3>
              样本外（test）· n = {results.summary.test_observations}
              <span className="status-pill">结论口径</span>
            </h3>
            {/* The observation count sits next to every statistic, not in a
                footnote. A Rank ICIR from sixteen weekly cross-sections is not
                the same claim as one from eight hundred, and separating the
                number from its sample size is how the former gets read as the
                latter. */}
            <table className="findings">
              <thead>
                <tr><th>指标</th><th>LightGBM</th><th>6-1 动量</th></tr>
              </thead>
              <tbody>
                {([
                  ['IC 均值', 'ic_mean'],
                  ['ICIR', 'icir'],
                  ['Rank IC 均值', 'rank_ic_mean'],
                  ['Rank ICIR', 'rank_icir'],
                  ['最高减最低组', 'long_short_mean'],
                  ['分组单调性', 'group_monotonicity'],
                ] as const).map(([label, key]) => (
                  <tr key={key}>
                    <td>{label}</td>
                    <td>{metric(testModel?.[key] as number | null)}</td>
                    <td>{metric(testMomentum?.[key] as number | null)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {/* Deliberately no verdict. On this many observations the difference
                between the two columns cannot be significant, and a headline
                saying which won would be the most-read and least-supported
                thing on the page. */}
            <p className="endpoint">
              两列共享同一股票域、标签、覆盖率门槛与统计代码，唯一差异是分数来源。
              J-Quants Free 约 2 年滚动历史，样本外仅 {results.summary.test_observations} 个周度观察点，
              据此判断优劣不具统计意义。
            </p>
          </div>

          <div className="research-results">
            <h3>样本内（train / valid）<span className="status-pill">不构成结论</span></h3>
            <table className="findings">
              <thead><tr><th>区间</th><th>来源</th><th>n</th><th>Rank IC</th><th>Rank ICIR</th><th>分组单调性</th></tr></thead>
              <tbody>
                {segments.filter((s) => s.segment !== 'test').map((s) => (
                  <tr key={`${s.segment}-${s.source}`}>
                    <td>{s.segment}</td>
                    <td>{s.source}</td>
                    <td>{s.observations}</td>
                    <td>{metric(s.rank_ic_mean)}</td>
                    <td>{metric(s.rank_icir)}</td>
                    <td>{metric(s.group_monotonicity)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="endpoint">
              样本内指标与样本外并列展示，是为了让过拟合可见：train 的 Rank IC 远高于 test 时，
              高的那个不是结论。
            </p>
          </div>

          <FeatureImportance
            rows={results.feature_importance}
            zeroGain={results.summary.zero_gain_features}
            expanded={showAllFeatures}
            onToggle={() => setShowAllFeatures((value) => !value)}
          />

          <MissingRates
            rows={results.feature_missing_rate}
            expanded={showAllMissing}
            onToggle={() => setShowAllMissing((value) => !value)}
          />

          <TrainingCurve rows={results.training_curve} bestIteration={results.summary.best_iteration} />
        </>
      )}

      {latest?.status === 'succeeded' && ranked && (
        <RankedScoresTable key={latest.id} ranked={ranked} />
      )}
    </section>
  )
}

function RankedScoresTable({ ranked }: { ranked: RankedScores }) {
  const dates = [...new Set(ranked.scores.map((row) => row.observation_date).filter(Boolean))]
    .sort((a, b) => b.localeCompare(a))
  const [selectedDate, setSelectedDate] = useState(ranked.observation_date ?? dates[0] ?? '')
  const [view, setView] = useState<'20' | '50' | 'all'>('20')
  const [page, setPage] = useState(1)

  const datedScores = dates.length
    ? ranked.scores.filter((row) => row.observation_date === selectedDate)
    : ranked.scores
  const sortedScores = [...datedScores].sort((a, b) => {
    const rankDifference = (b.rank_percentile ?? -1) - (a.rank_percentile ?? -1)
    return rankDifference || a.instrument_id.localeCompare(b.instrument_id)
  })
  const pageCount = Math.max(1, Math.ceil(sortedScores.length / SCORE_PAGE_SIZE))
  const shownScores = view === 'all'
    ? sortedScores.slice((page - 1) * SCORE_PAGE_SIZE, page * SCORE_PAGE_SIZE)
    : sortedScores.slice(0, Number(view))

  function chooseDate(value: string) {
    setSelectedDate(value)
    setPage(1)
  }

  function chooseView(value: '20' | '50' | 'all') {
    setView(value)
    setPage(1)
  }

  return (
    <div className="ranked-scores">
      <div className="ranked-heading">
        <h3>RankedScores · {selectedDate || '样本外截面'}</h3>
        <label>
          预测日期
          <select aria-label="预测日期" value={selectedDate} onChange={(event) => chooseDate(event.target.value)}>
            {dates.map((date) => <option key={date} value={date}>{date}</option>)}
          </select>
        </label>
      </div>
      <div className="score-view-controls" role="group" aria-label="排名显示范围">
        {(['20', '50', 'all'] as const).map((value) => (
          <button
            type="button"
            className={view === value ? 'primary' : 'secondary'}
            aria-pressed={view === value}
            key={value}
            onClick={() => chooseView(value)}
          >
            {value === 'all' ? '全部' : `Top ${value}`}
          </button>
        ))}
      </div>
      <p className="endpoint">
        默认展示所选预测日的头部排名；这是界面视图，不会截断 API、研究产物或替代组合策略（08）。
        位次百分位使用百分号显示，表示截面内相对位置，不是预期收益。
      </p>
      <table className="findings">
        <thead><tr><th>证券代码</th><th>证券身份</th><th>原始分数</th><th>平均秩</th><th>位次百分位</th><th>标签状态</th></tr></thead>
        <tbody>{shownScores.map((row) => (
          <tr key={`${selectedDate}-${row.instrument_id}`}>
            {/* Code first because it is the only column a person can read, but
                instrument_id stays on the row: it is the identity 08 keys on,
                and a ticker can be reassigned between dates. */}
            <td>{row.source_code ?? '—'}</td>
            <td className="instrument-id">{row.instrument_id}</td>
            <td>{metric(row.raw_score, 6)}</td>
            <td>{row.average_rank ?? '—'}</td>
            <td>{row.rank_percentile == null ? '—' : row.rank_percentile.toLocaleString('zh-CN', { style: 'percent', maximumFractionDigits: 1 })}</td>
            <td>{row.label_status}</td>
          </tr>
        ))}</tbody>
      </table>
      {view === 'all' && pageCount > 1 && (
        <nav className="score-pagination" aria-label="排名分页">
          <button type="button" className="secondary" disabled={page === 1} onClick={() => setPage((value) => value - 1)}>上一页</button>
          <span>第 {page} / {pageCount} 页 · 共 {sortedScores.length} 条</span>
          <button type="button" className="secondary" disabled={page === pageCount} onClick={() => setPage((value) => value + 1)}>下一页</button>
        </nav>
      )}
    </div>
  )
}

function FeatureImportance({
  rows, zeroGain, expanded, onToggle,
}: {
  rows: ModelResults['feature_importance']
  zeroGain: number
  expanded: boolean
  onToggle: () => void
}) {
  const shown = expanded ? rows : rows.slice(0, 20)
  return (
    <div className="research-results">
      <div className="run-title">
        <h3>特征重要性</h3>
        <button className="secondary" onClick={onToggle}>{expanded ? '只看前 20' : `展开全部 ${rows.length} 列`}</button>
      </div>
      {/* gain and split together: split flatters high-cardinality continuous
          columns, which are simply easier to keep splitting on, so the pair is
          what reveals a feature used often that contributes nothing. */}
      <p className="endpoint">
        gain 为贡献的损失下降总量，split 为被选作分裂点的次数；
        其中 <strong>{zeroGain}</strong> 列 gain 为 0，即完全未被使用。
      </p>
      <table className="findings">
        <thead><tr><th>#</th><th>特征</th><th>gain</th><th>split</th></tr></thead>
        <tbody>{shown.map((row) => (
          <tr key={row.feature_name}>
            <td>{row.gain_rank}</td><td>{row.feature_name}</td>
            <td>{row.gain.toFixed(2)}</td><td>{row.split}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  )
}

function MissingRates({
  rows, expanded, onToggle,
}: {
  rows: ModelResults['feature_missing_rate']
  expanded: boolean
  onToggle: () => void
}) {
  const byFeature = new Map<string, Record<string, number>>()
  for (const row of rows) {
    const entry = byFeature.get(row.feature_name) ?? {}
    entry[row.segment] = row.missing_rate
    byFeature.set(row.feature_name, entry)
  }
  const all = [...byFeature.entries()].map(([name, segments]) => ({
    name,
    train: segments.train ?? 0,
    valid: segments.valid ?? 0,
    test: segments.test ?? 0,
    drift: (segments.test ?? 0) - (segments.train ?? 0),
  }))
  // Only the columns that drifted, by default. A 158-row table is one nobody
  // reads; the dozen that got worse between train and test are the ones that
  // point at a data problem rather than a model.
  const drifted = all.filter((row) => row.drift > MISSING_RATE_DRIFT).sort((a, b) => b.drift - a.drift)
  const shown = expanded ? all : drifted

  return (
    <div className="research-results">
      <div className="run-title">
        <h3>特征缺失率</h3>
        <button className="secondary" onClick={onToggle}>{expanded ? '只看异常项' : `展开全部 ${all.length} 列`}</button>
      </div>
      <p className="endpoint">
        本设计不填充缺失值，交由 LightGBM 原生处理，因此缺失结构必须可见。
        默认只列出 test 段比 train 段高出 10 个百分点以上的列。
      </p>
      {shown.length === 0 ? <p className="empty">没有列出现显著的 train → test 缺失率漂移。</p> : (
        <table className="findings">
          <thead><tr><th>特征</th><th>train</th><th>valid</th><th>test</th><th>漂移</th></tr></thead>
          <tbody>{shown.slice(0, 200).map((row) => (
            <tr key={row.name}>
              <td>{row.name}</td>
              <td>{(row.train * 100).toFixed(1)}%</td>
              <td>{(row.valid * 100).toFixed(1)}%</td>
              <td>{(row.test * 100).toFixed(1)}%</td>
              <td>{(row.drift * 100).toFixed(1)}pp</td>
            </tr>
          ))}</tbody>
        </table>
      )}
    </div>
  )
}

function TrainingCurve({
  rows, bestIteration,
}: {
  rows: ModelResults['training_curve']
  bestIteration: number | null
}) {
  if (rows.length === 0) return null
  const values = rows.map((row) => row.value)
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  return (
    <div className="research-results">
      <h3>训练曲线 · best_iteration = {bestIteration ?? 'unavailable'}</h3>
      <table className="findings">
        <thead><tr><th>轮次</th><th>数据集</th><th>{rows[0].metric}</th><th /></tr></thead>
        <tbody>{rows.filter((_, index) => index % Math.max(1, Math.floor(rows.length / 20)) === 0).map((row) => (
          <tr key={`${row.dataset}-${row.iteration}`}>
            <td>{row.iteration}</td>
            <td>{row.dataset}</td>
            <td>{row.value.toFixed(6)}</td>
            <td>
              <div className="progress-track" aria-hidden>
                <span style={{ width: `${((row.value - min) / span) * 100}%` }} />
              </div>
            </td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  )
}
