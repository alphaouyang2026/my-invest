import { afterEach, expect, test, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import ResearchCenter from '../app/research-center'

afterEach(() => vi.unstubAllGlobals())

const ok = (body: unknown) => ({ ok: true, json: async () => body })

test('user can create the default momentum experiment and see its fixed evaluation rules', async () => {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === 'POST') {
      return ok({ id: 'run-1', experiment_id: 'experiment-1', status: 'queued', processed_dates: 0, total_dates: 0, warnings: [] })
    }
    if (url.includes('/research/config')) {
      return ok({
        default_snapshot: { id: 'snapshot-1', version: 3 },
        observation_start: '2024-08-01',
        observation_end: '2025-07-31',
        evaluation_end: '2025-07-23',
        lookback_days: 126,
        skip_days: 21,
        factor_coverage: 0.9,
        label_coverage: 0.9,
        min_valid_securities: 100,
      })
    }
    if (url.includes('/research/runs')) return ok([])
    if (url.includes('/qlib-data-bundles')) return ok([])
    return ok([])
  })
  vi.stubGlobal('fetch', fetchMock)

  render(<ResearchCenter />)

  expect(await screen.findByRole('heading', { name: 'Qlib 动量因子研究' })).toBeInTheDocument()
  expect(await screen.findByText(/日度标签：下一开盘至第六个后续开盘/)).toBeInTheDocument()
  expect(screen.getByText(/因子与标签覆盖率均需达到 90%/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '创建研究运行' }))

  await waitFor(() => {
    const call = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(call?.[0]).toContain('/research/runs')
    expect(JSON.parse(call?.[1]?.body as string)).toEqual({
      data_snapshot_id: 'snapshot-1',
      observation_start: '2024-08-01',
      observation_end: '2025-07-31',
      lookback_days: 126,
      skip_days: 21,
    })
  })
  expect(await screen.findByText('queued')).toBeInTheDocument()
})

test('user can inspect daily diagnostics and manage Qlib data bundles', async () => {
  const succeeded = {
    id: 'run-1', experiment_id: 'experiment-1', status: 'succeeded', processed_dates: 10,
    total_dates: 10, warnings: [], summary: { effective_factor_end: '2025-03-31' },
  }
  const bundle = {
    id: 'bundle-1', data_snapshot_id: 'snapshot-1', status: 'ready', pyqlib_version: '0.9.7',
    size_bytes: 1048576, deletable: true,
  }
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.includes('/research/config')) return ok({
      default_snapshot: { id: 'snapshot-1', version: 3 }, observation_start: '2024-08-01',
      observation_end: '2025-03-31', evaluation_end: '2025-03-21', lookback_days: 126,
      skip_days: 21, factor_coverage: 0.9, label_coverage: 0.9, min_valid_securities: 100,
    })
    if (url.includes('/research/runs/run-1/results')) return ok({
      summary: { effective_factor_end: '2025-03-31' },
      daily_metrics: [{ observation_date: '2025-03-31', ic: 0.12, rank_ic: 0.11, factor_coverage: 0.95, label_coverage: 0.94, long_short_return: 0.02 }],
      weekly_metrics: [],
    })
    if (url.includes('/ranked-scores')) return ok({ observation_date: '2025-03-31', scores: [], exclusions: [{ instrument_id: 'instrument-1', reason: 'insufficient_turnover' }] })
    if (url.includes('/research/runs')) return ok([succeeded])
    if (url.includes('/qlib-data-bundles') && init?.method === 'DELETE') return ok({ ...bundle, status: 'deleted' })
    if (url.includes('/qlib-data-bundles') && init?.method === 'POST') return ok({ ...bundle, status: 'queued' })
    if (url.includes('/qlib-data-bundles')) return ok([bundle])
    return ok([])
  })
  vi.stubGlobal('fetch', fetchMock)

  render(<ResearchCenter />)

  expect(await screen.findByRole('heading', { name: '日度诊断' })).toBeInTheDocument()
  expect(await screen.findByText('insufficient_turnover')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '删除数据包' }))
  fireEvent.click(screen.getByRole('button', { name: '预构建当前快照' }))

  await waitFor(() => {
    expect(fetchMock.mock.calls.some(([url, init]) => String(url).includes('/bundle-1') && init?.method === 'DELETE')).toBe(true)
    expect(fetchMock.mock.calls.some(([url, init]) => String(url).endsWith('/qlib-data-bundles') && init?.method === 'POST')).toBe(true)
  })
})
