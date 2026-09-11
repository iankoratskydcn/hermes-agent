/**
 * Per-board dispatch toggles: inline header controls on KanbanBoardPage
 * (moved out of the removed BoardSettingsDialog — see board.tsx's
 * `toggleBoardSetting` mutation). Covers default-board resolution, exact
 * single-field PATCH bodies, authoritative-response reconciliation (not the
 * locally-sent patch), double-click serialization, and failed-write feedback
 * without a false-success UI state.
 */
import type { PluginRestOptions } from '@hermes/plugin-sdk'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Test harness supplies the host's locale registration, as plugin loading does.
// eslint-disable-next-line no-restricted-imports
import { registerPluginLocales } from '@/i18n/plugin-i18n'

import { $boardSlug, bindApi } from './api'
import { KanbanBoardPage } from './board'
import { KANBAN_LOCALES } from './i18n'
import type { BoardMeta } from './types'

vi.mock('@/hermes', () => ({ setApiRequestProfile: vi.fn() }))

const emptyBoard = { columns: [], tenants: [], assignees: [], latest_event_id: 0, now: 0 }

let boards: BoardMeta[]
let current: string
let client: QueryClient
let disposeApi: () => void
let disposeLocales: () => void
let patched: Array<{ slug: string; patch: Record<string, unknown> }>
let patchDelay: null | (() => void)

const rest = vi.fn(async (path: string, options?: PluginRestOptions): Promise<unknown> => {
  if (path === '/board' || path.startsWith('/board?')) {
    return emptyBoard
  }

  if (path === '/boards') {
    return { boards: boards.map(b => ({ ...b })), current }
  }

  const patchMatch = /^\/boards\/([^/]*)$/.exec(path)

  if (patchMatch && options?.method === 'PATCH') {
    const slug = decodeURIComponent(patchMatch[1])
    const patch = options.body as Record<string, unknown>

    patched.push({ slug, patch })

    if (patchDelay) {
      await new Promise<void>(resolve => {
        patchDelay = resolve
      })
    }

    const idx = boards.findIndex(b => b.slug === slug)
    if (idx < 0) {
      throw new Error(`no such board: ${slug}`)
    }

    boards[idx] = { ...boards[idx], ...patch }
    return { board: boards[idx] }
  }

  if (path === '/profiles') {
    return { profiles: [] }
  }

  if (path === '/orchestration') {
    return { default_assignee: '' }
  }

  throw new Error(`Unexpected REST request: ${path} ${options?.method ?? 'GET'}`)
})

beforeEach(() => {
  patched = []
  patchDelay = null
  boards = [{ slug: 'proj', name: 'Proj', total: 3 }]
  current = 'proj'
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  disposeLocales = registerPluginLocales('kanban', KANBAN_LOCALES)
  disposeApi = bindApi(
    async <T,>(path: string, options?: PluginRestOptions) => (await rest(path, options)) as T,
    { get: (_key, fallback) => fallback, set: vi.fn(), remove: vi.fn() },
    () => vi.fn()
  )
  $boardSlug.set('')
})

afterEach(() => {
  cleanup()
  client.clear()
  disposeApi()
  disposeLocales()
  vi.clearAllMocks()
})

function openPage() {
  return render(
    <QueryClientProvider client={client}>
      <KanbanBoardPage />
    </QueryClientProvider>
  )
}

describe('per-board dispatch toggles (inline header controls)', () => {
  it('resolves and shows toggles for the DEFAULT board even with an empty $boardSlug', async () => {
    // $boardSlug.set('') means "server current board" per api.ts's contract;
    // this reproduces the exact regression the independent audit found —
    // currentBoard used to be undefined here and the switches never rendered.
    openPage()

    expect(await screen.findByRole('switch', { name: /dispatch/i })).toBeTruthy()
  })

  it('defaults every toggle to on when the backend omits the flags (old board.json)', async () => {
    openPage()

    const dispatchSwitch = await screen.findByRole('switch', { name: /dispatch/i })
    const decomposeSwitch = await screen.findByRole('switch', { name: /decompose/i })
    const reviewSwitch = await screen.findByRole('switch', { name: /review/i })

    expect(dispatchSwitch.getAttribute('aria-checked')).toBe('true')
    expect(decomposeSwitch.getAttribute('aria-checked')).toBe('true')
    expect(reviewSwitch.getAttribute('aria-checked')).toBe('true')
  })

  it('turning a switch off PATCHes the resolved current-board slug with exactly that field', async () => {
    openPage()

    const dispatchSwitch = await screen.findByRole('switch', { name: /dispatch/i })
    fireEvent.click(dispatchSwitch)

    await waitFor(() => expect(patched).toContainEqual({ slug: 'proj', patch: { dispatch_enabled: false } }))
    // The empty local $boardSlug override must resolve to the real board —
    // never PATCH /boards/ (empty slug).
    expect(patched.every(p => p.slug === 'proj')).toBe(true)
    // The other two flags are untouched by this write.
    expect(patched.some(p => 'auto_decompose_enabled' in p.patch || 'review_dispatch_enabled' in p.patch)).toBe(false)
  })

  it('reflects the server RESPONSE value after the toggle round-trips (not the locally-sent patch)', async () => {
    // The mock server intentionally does NOT echo back exactly what was sent —
    // it also flips a different field server-side, proving the UI reconciles
    // from the authoritative response body, not an optimistic guess.
    const originalRest = rest.getMockImplementation()!
    rest.mockImplementation(async (path, options) => {
      const patchMatch = /^\/boards\/([^/]*)$/.exec(path)
      if (patchMatch && options?.method === 'PATCH') {
        const slug = decodeURIComponent(patchMatch[1])
        const idx = boards.findIndex(b => b.slug === slug)
        boards[idx] = { ...boards[idx], ...(options.body as Record<string, unknown>), review_dispatch_enabled: false }
        patched.push({ slug, patch: options.body as Record<string, unknown> })
        return { board: boards[idx] }
      }
      return originalRest(path, options)
    })

    openPage()
    const dispatchSwitch = await screen.findByRole('switch', { name: /dispatch/i })
    fireEvent.click(dispatchSwitch)

    await waitFor(async () => {
      const reviewSwitch = await screen.findByRole('switch', { name: /review/i })
      expect(reviewSwitch.getAttribute('aria-checked')).toBe('false')
    })
  })

  it('serializes double-clicks: a second click on the SAME field while pending does not send a second PATCH', async () => {
    const release: { fn: null | (() => void) } = { fn: null }
    patchDelay = () => {}
    rest.mockImplementation(async (path, options) => {
      if (path === '/board' || path.startsWith('/board?')) return emptyBoard
      if (path === '/boards') return { boards: boards.map(b => ({ ...b })), current }
      const patchMatch = /^\/boards\/([^/]*)$/.exec(path)
      if (patchMatch && options?.method === 'PATCH') {
        const slug = decodeURIComponent(patchMatch[1])
        patched.push({ slug, patch: options.body as Record<string, unknown> })
        await new Promise<void>(resolve => {
          release.fn = resolve
        })
        const idx = boards.findIndex(b => b.slug === slug)
        boards[idx] = { ...boards[idx], ...(options.body as Record<string, unknown>) }
        return { board: boards[idx] }
      }
      throw new Error(`Unexpected REST request: ${path}`)
    })

    openPage()
    const dispatchSwitch = await screen.findByRole('switch', { name: /dispatch/i })
    fireEvent.click(dispatchSwitch)
    await waitFor(() => expect(patched.length).toBe(1))

    // Same field clicked again while the first write is still in flight.
    fireEvent.click(dispatchSwitch)
    expect(patched.length).toBe(1)

    release.fn?.()
    await waitFor(() => expect(dispatchSwitch.getAttribute('aria-checked')).toBe('false'))
  })

  it('a failed write shows an error and does NOT report false success (switch reverts, no phantom update)', async () => {
    rest.mockImplementation(async (path, options) => {
      if (path === '/board' || path.startsWith('/board?')) return emptyBoard
      if (path === '/boards') return { boards: boards.map(b => ({ ...b })), current }
      const patchMatch = /^\/boards\/([^/]*)$/.exec(path)
      if (patchMatch && options?.method === 'PATCH') {
        throw new Error('simulated server rejection')
      }
      throw new Error(`Unexpected REST request: ${path}`)
    })

    openPage()
    const dispatchSwitch = await screen.findByRole('switch', { name: /dispatch/i })
    fireEvent.click(dispatchSwitch)

    // The board's own dispatch_enabled must never have been mutated locally —
    // no optimistic Object.assign that could report a phantom success.
    await waitFor(() => expect(boards[0].dispatch_enabled).not.toBe(false))
    await waitFor(() => expect(dispatchSwitch.getAttribute('aria-checked')).toBe('true'))
  })
})
