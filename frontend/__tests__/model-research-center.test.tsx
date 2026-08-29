import { afterEach, expect, test, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import ModelResearchCenter from '../app/model-research-center'

afterEach(() => vi.unstubAllGlobals())

const ok = (body: unknown) => ({ ok: true, json: async () => body })

const CONFIG = {
  default_snapshot: { id: 'snapshot-1', version: 3 },
  default_feature_set: 'alpha158_jp_v1',
  default_seed: 20260829,
  locked_params: { objective: 'mse', deterministic: true, force_row_wise: true },
  overridable_params: { learning_rate: { type: 'float', min: 0, max: 0.5 } },
  splits: {
    train_start: '2025-01-10', train_end: '2025-05-02',
    valid_start: '2025-05-16', valid_end: '2025-06-20',
    test_start: '2025-07-04', test_end: '2025-08-01',
  },
  feature_sets: [
    { name: 'alpha158_jp_v1', version: '1', description: '', column_count: 158, max_window: 60, selectable: true, is_default: true, maturity: 'baseline' },
    { name: 'alpha360_jp_v1', version: '1', description: '', column_count: 360, max_window: 59, selectable: true, is_default: false, maturity: 'experimental' },
    { name: 'momentum_only_v1', version: '1', description: '', column_count: 1, max_window: 147, selectable: false, is_default: false, maturity: 'baseline' },
  ],
}

const SUCCEEDED_RUN = {
  id: 'run-1', status: 'succeeded', processed_dates: 16, total_dates: 16,
  warnings: [{ code: 'free_data_limit', detail: 'J-Quants Free history limits inference' }],
  summary: { test_observations: 16 },
}

const segment = (seg: string, source: string, extra: Record<string, unknown> = {}) => ({
  segment: seg, source, observations: seg === 'test' ? 16 : 50,
  ic_mean: 0.01, icir: 0.2, rank_ic_mean: 0.02, rank_icir: 0.3,
  long_short_mean: 0.001, group_monotonicity: 0.9, ...extra,
})

const RESULTS = {
  summary: {
    segments: [
      segment('train', 'lightgbm', { rank_ic_mean: 0.15 }),
      segment('train', 'momentum_6_1'),
      segment('valid', 'lightgbm'),
      segment('valid', 'momentum_6_1'),
      segment('test', 'lightgbm'),
      segment('test', 'momentum_6_1'),
    ],
    test_observations: 16,
    best_iteration: 42,
    zero_gain_features: 37,
    feature_set: 'alpha158_jp_v1',
  },
  trained_model: null,
  prediction_run: null,
  feature_importance: Array.from({ length: 30 }, (_, index) => ({
    feature_name: `F${index}`, gain: 100 - index, split: 10, gain_rank: index + 1,
  })),
  feature_missing_rate: [
    { segment: 'train', feature_name: 'STABLE', missing_rate: 0.02 },
    { segment: 'valid', feature_name: 'STABLE', missing_rate: 0.02 },
    { segment: 'test', feature_name: 'STABLE', missing_rate: 0.03 },
    { segment: 'train', feature_name: 'DRIFTED', missing_rate: 0.03 },
    { segment: 'valid', feature_name: 'DRIFTED', missing_rate: 0.20 },
    { segment: 'test', feature_name: 'DRIFTED', missing_rate: 0.40 },
  ],
  training_curve: [{ dataset: 'valid', metric: 'l2', iteration: 0, value: 0.9 }],
}

function stubApi(overrides: Record<string, unknown> = {}) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === 'POST') return ok({ id: 'run-1', status: 'queued', processed_dates: 0, total_dates: 0, warnings: [] })
    if (url.includes('/research/model-runs/config')) return ok({ ...CONFIG, ...overrides })
    if (url.includes('/results')) return ok(RESULTS)
    if (url.includes('/ranked-scores')) return ok({ observation_date: null, scores: [] })
    if (url.includes('/research/runs')) return ok([SUCCEEDED_RUN])
    return ok([])
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

test('the creation form submits explicit segment dates and a top-level seed', async () => {
  const fetchMock = stubApi()

  render(<ModelResearchCenter />)
  await screen.findByRole('option', { name: /alpha158_jp_v1/ })
  fireEvent.click(screen.getByRole('button', { name: '开始训练' }))

  await waitFor(() => {
    const call = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(call?.[0]).toContain('/research/model-runs')
    const body = JSON.parse(call?.[1]?.body as string)
    // Dates, not ratios: what is submitted is what gets fingerprinted.
    expect(body.train_start).toBe('2025-01-10')
    expect(body.test_end).toBe('2025-08-01')
    // One entry point for the seed, so no precedence rule has to exist.
    expect(body.seed).toBe(20260829)
    expect(body.model_params).toBeUndefined()
  })
})

test('the momentum control is not offered as something to train on', async () => {
  stubApi()

  render(<ModelResearchCenter />)
  // The heading is static and renders before the config arrives; waiting on an
  // option is what proves the feature sets actually loaded.
  expect(await screen.findByRole('option', { name: /alpha158_jp_v1/ })).toBeInTheDocument()
  expect(screen.getByRole('option', { name: /alpha360_jp_v1/ })).toBeInTheDocument()
  expect(screen.queryByRole('option', { name: /momentum_only_v1/ })).not.toBeInTheDocument()
})

test('choosing alpha360 shows that it is experimental', async () => {
  stubApi()

  render(<ModelResearchCenter />)
  await screen.findByRole('option', { name: /alpha360_jp_v1/ })
  fireEvent.change(screen.getByRole('combobox'), { target: { value: 'alpha360_jp_v1' } })

  expect(await screen.findByText(/实验性特征集/)).toBeInTheDocument()
})

test('the locked parameters are shown, not merely enforced by the API', async () => {
  stubApi()

  render(<ModelResearchCenter />)

  expect(await screen.findByText(/objective、deterministic、force_row_wise/)).toBeInTheDocument()
})

test('the observation count sits next to the out-of-sample metrics', async () => {
  stubApi()

  render(<ModelResearchCenter />)

  // Sixteen weekly cross-sections is not the same claim as eight hundred, and
  // the heading is where a reader can see which one this is.
  expect(await screen.findByText(/样本外（test）· n = 16/)).toBeInTheDocument()
  expect(screen.getByText(/仅 16 个周度观察点/)).toBeInTheDocument()
})

test('the page never declares a winner between the model and momentum', async () => {
  stubApi()

  render(<ModelResearchCenter />)
  await screen.findByText(/样本外（test）· n = 16/)

  const text = document.body.textContent ?? ''
  for (const verdict of ['优于', '胜出', '更好', '战胜', '跑赢']) {
    expect(text).not.toContain(verdict)
  }
})

test('in-sample metrics are labelled as not being a conclusion', async () => {
  stubApi()

  render(<ModelResearchCenter />)

  expect(await screen.findByText('不构成结论')).toBeInTheDocument()
  expect(screen.getByText(/高的那个不是结论/)).toBeInTheDocument()
})

test('feature importance reports both measures and how many columns went unused', async () => {
  stubApi()

  render(<ModelResearchCenter />)

  expect(await screen.findByText(/gain 为 0，即完全未被使用/)).toBeInTheDocument()
  expect(screen.getByText('37')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /展开全部 30 列/ })).toBeInTheDocument()
})

test('missing rates show only the drifted columns until asked for the rest', async () => {
  stubApi()

  render(<ModelResearchCenter />)
  await screen.findByText(/本设计不填充缺失值/)

  // A 158-row table is one nobody reads; the columns that got worse between
  // train and test are the ones that point at a data problem.
  expect(screen.getByText('DRIFTED')).toBeInTheDocument()
  expect(screen.queryByText('STABLE')).not.toBeInTheDocument()

  fireEvent.click(screen.getByRole('button', { name: /展开全部 2 列/ }))
  expect(await screen.findByText('STABLE')).toBeInTheDocument()
})

test('the page says scores carry no return unit', async () => {
  stubApi()

  render(<ModelResearchCenter />)

  expect(await screen.findByText(/没有收益量纲/)).toBeInTheDocument()
})
