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

test('configured data source can start a sync and shows the active run', async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(ok(CONFIGURED))
    .mockResolvedValueOnce(ok([]))
    .mockResolvedValueOnce(
      ok({ id: 'run-1', status: 'queued', phase: 'discovering_calendar', target_dates: 0, processed_dates: 0 }),
    )
  vi.stubGlobal('fetch', fetchMock)

  render(<DataCenter />)

  const button = await screen.findByRole('button', { name: '立即同步' })
  await waitFor(() => expect(button).toBeEnabled())
  fireEvent.click(button)

  await waitFor(() => expect(screen.getAllByText('queued').length).toBeGreaterThan(0))
})

test('sync now sends no execution parameters', async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(ok(CONFIGURED))
    .mockResolvedValueOnce(ok([]))
    .mockResolvedValueOnce(ok({ id: 'run-1', status: 'queued' }))
  vi.stubGlobal('fetch', fetchMock)

  render(<DataCenter />)
  const button = await screen.findByRole('button', { name: '立即同步' })
  await waitFor(() => expect(button).toBeEnabled())
  fireEvent.click(button)

  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3))
  const [url, init] = fetchMock.mock.calls[2]
  expect(url).toContain('/data-sync/jquants')
  expect(JSON.parse(init.body)).toEqual({})
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
  vi.stubGlobal(
    'fetch',
    vi.fn()
      .mockResolvedValueOnce(ok({ ...CONFIGURED, latest_run: running }))
      .mockResolvedValueOnce(ok([running])),
  )

  render(<DataCenter />)

  expect(await screen.findByText('27 / 98')).toBeInTheDocument()
  expect(screen.getByText('135 / 487')).toBeInTheDocument()
  expect(screen.getByText(/同步日线/)).toBeInTheDocument()
  expect(screen.getByText(/正在取 2025-01-16/)).toBeInTheDocument()
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
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(ok({ ...CONFIGURED, latest_run: partial }))
    .mockResolvedValueOnce(ok([partial]))
    .mockResolvedValueOnce(ok({ ...partial, status: 'queued' }))
  vi.stubGlobal('fetch', fetchMock)

  render(<DataCenter />)

  const resume = await screen.findByRole('button', { name: '恢复运行' })
  expect(screen.queryByRole('button', { name: '取消任务' })).toBeNull()
  fireEvent.click(resume)

  await waitFor(() => expect(fetchMock.mock.calls[2][0]).toContain('/data-sync/runs/run-3/resume'))
})

test('a non-resumable run offers no resume button', async () => {
  const failed = { id: 'run-4', status: 'failed', resumable: false, error_summary: '空的首次导入' }
  vi.stubGlobal(
    'fetch',
    vi.fn()
      .mockResolvedValueOnce(ok({ ...CONFIGURED, latest_run: failed }))
      .mockResolvedValueOnce(ok([failed])),
  )

  render(<DataCenter />)

  // The status shows twice by design: once on the run card, once in history.
  await waitFor(() => expect(screen.getAllByText('failed').length).toBeGreaterThan(0))
  expect(screen.queryByRole('button', { name: '恢复运行' })).toBeNull()
})
