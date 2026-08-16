import { afterEach, expect, test, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import DataCenter from '../app/data-center'

afterEach(() => vi.unstubAllGlobals())

const ok = (body: unknown) => ({ ok: true, json: async () => body })

const CONFIGURED = {
  configuration: 'configured',
  plan_notice: 'Free 数据约延迟 12 周',
  latest_run: null,
}

/**
 * Routed by URL rather than by call order: the page loads several endpoints on
 * mount, and a sequential mock silently hands the wrong body to the wrong
 * request the moment one is added.
 */
function mockApi(routes: Record<string, unknown>) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === 'POST') return ok(routes.POST ?? {})
    const match = Object.keys(routes)
      .filter((path) => path !== 'POST')
      .sort((a, b) => b.length - a.length)
      .find((path) => url.includes(path))
    return ok(match ? routes[match] : [])
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

test('configured data source can start a sync and shows the active run', async () => {
  mockApi({
    '/data-sync/status': CONFIGURED,
    '/data-sync/runs': [],
    '/snapshots': [],
    POST: { id: 'run-1', status: 'queued', phase: 'discovering_calendar', target_dates: 0, processed_dates: 0 },
  })

  render(<DataCenter />)

  const button = await screen.findByRole('button', { name: '立即同步' })
  await waitFor(() => expect(button).toBeEnabled())
  fireEvent.click(button)

  await waitFor(() => expect(screen.getAllByText('queued').length).toBeGreaterThan(0))
})

test('sync now sends no execution parameters', async () => {
  const fetchMock = mockApi({
    '/data-sync/status': CONFIGURED,
    '/data-sync/runs': [],
    '/snapshots': [],
    POST: { id: 'run-1', status: 'queued' },
  })

  render(<DataCenter />)
  const button = await screen.findByRole('button', { name: '立即同步' })
  await waitFor(() => expect(button).toBeEnabled())
  fireEvent.click(button)

  await waitFor(() => {
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(post).toBeDefined()
    expect(post![0]).toContain('/data-sync/jquants')
    expect(JSON.parse(post![1].body)).toEqual({})
  })
})

test('batch progress and phase are shown for a running sync', async () => {
  const running = {
    id: 'run-2',
    status: 'running',
    phase: 'bars',
    mode: 'initial',
    target_dates: 487,
    processed_dates: 135,
    total_batches: 98,
    completed_batches: 27,
    current_batch: 28,
    current_date: '2025-01-16',
    rows_new: 120000,
    rows_changed: 12,
    actual_min: '2024-01-04',
    actual_max: '2025-01-15',
  }
  mockApi({
    '/data-sync/status': { ...CONFIGURED, latest_run: running },
    '/data-sync/runs': [running],
    '/snapshots': [],
  })

  render(<DataCenter />)

  expect(await screen.findByText('27 / 98')).toBeInTheDocument()
  expect(screen.getByText('135 / 487')).toBeInTheDocument()
  expect(screen.getByText(/同步日线/)).toBeInTheDocument()
  expect(screen.getByText(/正在取 2025-01-16/)).toBeInTheDocument()
})

test('the quality phase is labelled rather than shown as a raw enum', async () => {
  const running = { id: 'run-5', status: 'running', phase: 'evaluating_quality', target_dates: 5, processed_dates: 5 }
  mockApi({
    '/data-sync/status': { ...CONFIGURED, latest_run: running },
    '/data-sync/runs': [running],
    '/snapshots': [],
  })

  render(<DataCenter />)

  expect(await screen.findByText(/校验数据质量/)).toBeInTheDocument()
})

test('a resumable terminal run offers resume instead of cancel', async () => {
  const partial = {
    id: 'run-3',
    status: 'partial_failed',
    phase: 'bars',
    resumable: true,
    target_dates: 12,
    processed_dates: 5,
    total_batches: 3,
    completed_batches: 1,
    error_summary: 'J-Quants daily bars request failed',
  }
  const fetchMock = mockApi({
    '/data-sync/status': { ...CONFIGURED, latest_run: partial },
    '/data-sync/runs': [partial],
    '/snapshots': [],
    POST: { ...partial, status: 'queued' },
  })

  render(<DataCenter />)

  const resume = await screen.findByRole('button', { name: '恢复运行' })
  expect(screen.queryByRole('button', { name: '取消任务' })).toBeNull()
  fireEvent.click(resume)

  await waitFor(() => {
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(post![0]).toContain('/data-sync/runs/run-3/resume')
  })
})

test('a non-resumable run offers no resume button', async () => {
  const failed = { id: 'run-4', status: 'failed', resumable: false, error_summary: '空的首次导入' }
  mockApi({
    '/data-sync/status': { ...CONFIGURED, latest_run: failed },
    '/data-sync/runs': [failed],
    '/snapshots': [],
  })

  render(<DataCenter />)

  // The status shows twice by design: once on the run card, once in history.
  await waitFor(() => expect(screen.getAllByText('failed').length).toBeGreaterThan(0))
  expect(screen.queryByRole('button', { name: '恢复运行' })).toBeNull()
})

test('a rejected snapshot is labelled and its reasons open on demand', async () => {
  const snapshot = {
    id: 'snap-1',
    version: 3,
    coverage_start: '2024-01-04',
    coverage_end: '2025-01-15',
    is_backtest_eligible: false,
    is_current: true,
  }
  mockApi({
    '/data-sync/status': CONFIGURED,
    '/data-sync/runs': [],
    '/snapshots/snap-1': {
      ...snapshot,
      verified_start: '2024-12-16',
      verified_end: '2025-01-15',
      findings: [
        {
          rule: 'negative_price',
          trade_date: '2025-01-10',
          severity: 'rejecting',
          affected_count: 40,
          evaluated_count: 2000,
          sample: ['13010', '13020'],
        },
      ],
    },
    '/snapshots': [snapshot],
  })

  render(<DataCenter />)

  const row = await screen.findByRole('button', { name: /v3/ })
  expect(screen.getByText('不可用于回测')).toBeInTheDocument()

  // A verdict with no rule and no date behind it would not be traceable.
  fireEvent.click(row)

  expect(await screen.findByText('价格为负')).toBeInTheDocument()
  expect(screen.getByText('2025-01-10')).toBeInTheDocument()
  expect(screen.getByText('40 / 2000')).toBeInTheDocument()
})
