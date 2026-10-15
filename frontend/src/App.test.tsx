import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { App } from '@/App'
import type { Issue, RequirementsState, Review } from '@/lib/api'
import { locationText } from '@/components/Shared'
import { reviewStageName } from '@/lib/stages'

const clients: QueryClient[] = []
const publicConfig = { max_files: 20, max_file_mb: 100, retention_days: 30, ocr_enabled: false, llm_configured: true }

const review: Review = {
  id: 'review-1', title: '项目方案.docx', status: 'completed', cancel_requested: false, stage: 'completed', progress_current: 5, progress_total: 5,
  issue_count: 1, created_at: '2026-10-15T02:00:00Z', finished_at: '2026-10-15T02:03:00Z', expires_at: '2026-11-14T02:03:00Z', error: null,
  files: [{ id: 'main', filename: '项目方案.docx', is_main: true, size: 1024 }, { id: 'ref', filename: '成本.xlsx', is_main: false, size: 2048 }],
  warnings: [], artifacts: [{ id: 'excel', name: 'report.xlsx', kind: 'xlsx', media_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' }],
}

const issue: Issue = {
  id: 'i1', category: 'numeric', scope: 'cross', severity: 'high', confidence: 'confirmed',
  title: '预算不一致', description: '主文件与附件的预算不同。', suggestion: '请核实并统一预算。',
  annotation_status: 'not_applicable', annotation_reason: '',
  evidence: [
    { document_id: 'main', block_id: 'b1', quote: '主文件预算100万元', location: { paragraph: 3, xpath: '/w:document/w:body/w:p[3]', char_start: 0 } },
    { document_id: 'ref', block_id: 'b2', quote: '附件预算120万元', location: { sheet: '成本', cell: 'B2', internal_field: 'private' } },
  ],
}

const readyRequirements: RequirementsState = {
  status: 'ready', raw_digest: 'raw', policy_version: 'v1', reasons: [],
  normalized: { digest: 'normalized', notes: ['预算按含税口径核对。'], custom_points: [{ id: 'custom-1', title: '核对税费', criterion: '预算应明确是否包含税费。', kind: 'required_content' }] },
}

function json(body: unknown, status = 200) { return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }) }

function renderApp(path = '/') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } })
  clients.push(client)
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><App /></MemoryRouter></QueryClientProvider>)
}

afterEach(() => { cleanup(); clients.forEach(client => client.clear()); clients.length = 0; vi.unstubAllGlobals(); vi.useRealTimers() })

describe('review workspace', () => {
  it('initializes a cookie session and uploads the selected main file with stable idempotency on retry', async () => {
    let attempts = 0
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === '/api/v1/session') return json({ ready: true, retention_days: 30 })
      if (url === '/api/v1/config') return json(publicConfig)
      if (url === '/api/v1/reviews' && init?.method === 'POST') {
        attempts++
        return attempts === 1 ? json({ detail: '队列暂时不可用' }, 503) : json({ id: 'review-1', status: 'queued' }, 202)
      }
      if (url.includes('/issues')) return json({ items: [], total: 0, page: 1, page_size: 20 })
      return json({ ...review, status: 'queued', stage: 'queued', artifacts: [], issue_count: 0 })
    })
    vi.stubGlobal('fetch', fetch)
    const user = userEvent.setup()
    renderApp()
    expect(await screen.findByRole('heading', { name: '新建审查' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '开始审查' })).toBeDisabled()
    const word = new File(['word'], '项目方案.docx', { type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' })
    const excel = new File(['excel'], '成本.xlsx', { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' })
    await user.upload(screen.getByLabelText('上传审查文件'), [word, excel])
    await user.click(screen.getAllByRole('radio')[1])
    await user.click(screen.getByRole('button', { name: '开始审查' }))
    expect(await screen.findByText('队列暂时不可用')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '开始审查' }))
    expect(await screen.findByText('等待审查资源')).toBeInTheDocument()
    const posts = fetch.mock.calls.filter(([url, init]) => url === '/api/v1/reviews' && init?.method === 'POST')
    const body = posts[0][1]?.body as FormData
    expect((body.get('main_file') as File).name).toBe('成本.xlsx')
    expect(body.getAll('reference_files').map(file => (file as File).name)).toEqual(['项目方案.docx'])
    expect(posts[0][1]?.headers).toEqual(posts[1][1]?.headers)
    expect(posts[0][1]?.credentials).toBe('same-origin')
    expect(fetch.mock.calls[0]).toEqual(['/api/v1/session', expect.objectContaining({ method: 'POST', credentials: 'same-origin' })])
  })

  it('rejects unsupported files and keeps the main selection valid after removing a file', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => json(url === '/api/v1/config' ? publicConfig : { ready: true, retention_days: 30 })))
    const user = userEvent.setup({ applyAccept: false })
    renderApp()
    await screen.findByRole('button', { name: '选择文件' })
    const input = screen.getByLabelText('上传审查文件')
    await user.upload(input, new File(['bad'], 'old.doc'))
    expect(await screen.findByRole('alert')).toHaveTextContent('格式不支持')
    expect(screen.getByRole('button', { name: '开始审查' })).toBeDisabled()
    await user.upload(input, [new File(['a'], 'a.pdf'), new File(['b'], 'b.pptx')])
    await user.click(screen.getAllByRole('radio')[1])
    await user.click(screen.getByRole('button', { name: '移除 a.pdf' }))
    expect(screen.queryByRole('radio')).not.toBeInTheDocument()
    expect(screen.getByText('主文件')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '开始审查' })).toBeEnabled()
  })

  it('renders both source quotations and uses authenticated download endpoints', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url === '/api/v1/session') return json({ ready: true, retention_days: 30 })
      if (url === '/api/v1/config') return json(publicConfig)
      if (url.includes('/issues')) return json({ items: [{ id: 'i1', category: 'numeric', scope: 'cross', severity: 'high', confidence: 'needs_confirmation', title: '项目预算不一致', description: '主文件预算与成本表总额不同。', suggestion: '请核实并统一项目预算。', annotation_status: 'added', annotation_reason: '', evidence: [{ document_id: 'main', block_id: 'b1', quote: '总预算为100万元。', location: { paragraph: 3 } }, { document_id: 'ref', block_id: 'b2', quote: '总预算120万元', location: { sheet: '成本', cell: 'B2' } }] }], total: 1, page: 1, page_size: 20 })
      return json(review)
    }))
    renderApp('/reviews/review-1')
    expect(await screen.findByText('项目预算不一致')).toBeInTheDocument()
    expect(screen.getByText('“总预算为100万元。”')).toBeInTheDocument()
    expect(screen.getByText('“总预算120万元”')).toBeInTheDocument()
    expect(screen.getByText('待确认')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '下载 Excel 报告' })).toHaveAttribute('href', '/api/v1/reviews/review-1/artifacts/excel')
    expect(screen.getByText('已添加到 DOCX 主文件批注')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /取消任务/ })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /项目预算不一致/ }))
    expect(screen.queryByText('“总预算为100万元。”')).not.toBeInTheDocument()
  })

  it('allows cancelling active work and retries partial results while showing coverage warnings', async () => {
    let current: Review = { ...review, status: 'running', stage: 'main', artifacts: [], issue_count: 0 }
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === '/api/v1/session') return json({ ready: true, retention_days: 30 })
      if (url === '/api/v1/config') return json(publicConfig)
      if (url.includes('/issues')) return json({ items: [], total: 0, page: 1, page_size: 20 })
      if (init?.method === 'POST' && url.endsWith('/cancel')) current = { ...current, status: 'partial', stage: 'partial', warnings: [{ code: 'empty_page', message: '第 2 页无法提取文字。', incomplete: true }] }
      if (init?.method === 'POST' && url.endsWith('/retry')) current = { ...current, status: 'queued', stage: 'queued' }
      return json(current)
    })
    vi.stubGlobal('fetch', fetch)
    const user = userEvent.setup()
    renderApp('/reviews/review-1')
    await user.click(await screen.findByRole('button', { name: '取消任务' }))
    expect(await screen.findByText('审查覆盖不完整')).toBeInTheDocument()
    expect(screen.getByText('第 2 页无法提取文字。')).not.toBeVisible()
    await user.click(screen.getByText('审查覆盖不完整'))
    expect(screen.getByText('第 2 页无法提取文字。')).toBeVisible()
    await user.click(screen.getByRole('button', { name: '重试任务' }))
    expect(await screen.findByText('等待审查资源')).toBeInTheDocument()
    expect(fetch).toHaveBeenCalledWith('/api/v1/reviews/review-1/retry', expect.objectContaining({ method: 'POST' }))
  })

  it('paginates history and deletes only after the confirmation dialog', async () => {
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === '/api/v1/session') return json({ ready: true, retention_days: 30 })
      if (url === '/api/v1/config') return json(publicConfig)
      if (init?.method === 'DELETE') return new Response(null, { status: 204 })
      return json({ items: [review], total: 11, page: url.includes('page=2') ? 2 : 1, page_size: 10 })
    })
    vi.stubGlobal('fetch', fetch)
    const user = userEvent.setup()
    renderApp('/history')
    await user.click(await screen.findByRole('button', { name: '下一页' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/v1/reviews?page=2&page_size=10', expect.anything()))
    await user.click(await screen.findByRole('button', { name: '删除 项目方案.docx' }))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(fetch.mock.calls.some(([, init]) => init?.method === 'DELETE')).toBe(false)
    await user.click(screen.getByRole('button', { name: '确认删除' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/v1/reviews/review-1', expect.objectContaining({ method: 'DELETE' })))
  })

  it('stops polling once the review is complete', async () => {
    const fetch = vi.fn(async (url: string) => {
      if (url === '/api/v1/session') return json({ ready: true, retention_days: 30 })
      if (url === '/api/v1/config') return json(publicConfig)
      if (url.includes('/issues')) return json({ items: [], total: 0, page: 1, page_size: 20 })
      return json(review)
    })
    vi.stubGlobal('fetch', fetch)
    renderApp('/reviews/review-1')
    await screen.findByText('审查完成')
    const count = fetch.mock.calls.filter(([url]) => url === '/api/v1/reviews/review-1').length
    vi.useFakeTimers()
    await act(async () => { await vi.advanceTimersByTimeAsync(4100) })
    expect(fetch.mock.calls.filter(([url]) => url === '/api/v1/reviews/review-1')).toHaveLength(count)
  })

  it('uses deployment limits and retention instead of fixed browser defaults', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => json(url === '/api/v1/config' ? { ...publicConfig, max_files: 1, max_file_mb: 1, retention_days: 7, llm_configured: false } : { ready: true, retention_days: 7 })))
    const user = userEvent.setup()
    renderApp()
    expect(await screen.findByText(/最多 1 份 · 单文件 1 MiB 以内/)).toBeInTheDocument()
    expect(screen.getByText('任务结束后保留 7 天，可随时下载或删除。')).toBeInTheDocument()
    expect(screen.getByText('审查模型服务尚未配置，请联系管理员完成设置后运行任务。')).toBeInTheDocument()
    await user.upload(screen.getByLabelText('上传审查文件'), [new File(['a'], 'a.pdf'), new File(['b'], 'b.docx')])
    expect(await screen.findByRole('alert')).toHaveTextContent('最多选择 1 份文件')
    expect(screen.getByRole('button', { name: '开始审查' })).toBeDisabled()
    await user.upload(screen.getByLabelText('上传审查文件'), new File([new Uint8Array(1024 * 1024 + 1)], 'large.pdf'))
    expect(await screen.findByRole('alert')).toHaveTextContent('不能超过 1 MiB')
  })

  it('retains selected files across configuration errors and enables submission after retry', async () => {
    let configured = false
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url === '/api/v1/config') return configured ? json(publicConfig) : json({ detail: '配置不可用' }, 503)
      return json({ ready: true, retention_days: 30 })
    }))
    const user = userEvent.setup()
    renderApp()
    await screen.findByText('无法读取审查配置，请重试。')
    await user.upload(screen.getByLabelText('上传审查文件'), new File(['a'], 'a.pdf'))
    expect(screen.getByRole('button', { name: '开始审查' })).toBeDisabled()
    configured = true
    await user.click(screen.getByRole('button', { name: '重试' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '开始审查' })).toBeEnabled())
    expect(screen.getByText('a.pdf')).toBeInTheDocument()
    expect(screen.queryByRole('radio')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '继续添加文件' })).toBeInTheDocument()
  })

  it('disables review submission when the model is unconfigured even with valid files', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => json(url === '/api/v1/config' ? { ...publicConfig, llm_configured: false } : { ready: true, retention_days: 30 })))
    const user = userEvent.setup()
    renderApp()
    await screen.findByText('审查模型服务尚未配置，请联系管理员完成设置后运行任务。')
    await user.upload(screen.getByLabelText('上传审查文件'), new File(['a'], 'a.pdf'))
    expect(screen.getByRole('button', { name: '开始审查' })).toBeDisabled()
  })

  it('clears validation when removing files and automatically selects the remaining main file', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => json(url === '/api/v1/config' ? publicConfig : { ready: true, retention_days: 30 })))
    const user = userEvent.setup({ applyAccept: false })
    renderApp()
    await screen.findByRole('button', { name: '选择文件' })
    const input = screen.getByLabelText('上传审查文件')
    await user.upload(input, [new File(['a'], 'a.pdf'), new File(['b'], 'b.docx')])
    await user.click(screen.getAllByRole('radio')[1])
    await user.upload(input, new File(['bad'], 'bad.txt'))
    expect(screen.getByRole('alert')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '移除 b.docx' }))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getByText('a.pdf')).toBeInTheDocument()
    expect(screen.getByText('主文件')).toBeInTheDocument()
  })

  it('restores cancellation in progress and labels progress as the current stage', async () => {
    const fetch = vi.fn(async (url: string) => {
      if (url === '/api/v1/session') return json({ ready: true, retention_days: 30 })
      if (url.includes('/issues')) return json({ items: [], total: 0, page: 1, page_size: 20 })
      return json({ ...review, status: 'running', stage: 'main', cancel_requested: true, progress_current: 2, progress_total: 7, artifacts: [], issue_count: 0 })
    })
    vi.stubGlobal('fetch', fetch)
    renderApp('/reviews/review-1')
    expect(await screen.findByRole('button', { name: '正在取消' })).toBeDisabled()
    expect(screen.getByText('本阶段已完成 2 / 7')).toBeInTheDocument()
    expect(screen.getByRole('progressbar', { name: '本阶段进度' })).toHaveAttribute('aria-valuemax', '7')
    expect(screen.queryByText(/%/)).not.toBeInTheDocument()
    const count = fetch.mock.calls.filter(([url]) => url === '/api/v1/reviews/review-1').length
    await waitFor(() => expect(fetch.mock.calls.filter(([url]) => url === '/api/v1/reviews/review-1').length).toBeGreaterThan(count), { timeout: 3000 })
  })

  it('opens only the first issue by default and retains expansion through refetches and pagination', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url === '/api/v1/session') return json({ ready: true, retention_days: 30 })
      if (url.includes('/issues')) return json({ items: url.includes('page=2') ? [{ ...issue, id: 'i3', title: '第三个问题' }] : [issue, { ...issue, id: 'i2', title: '第二个问题' }], total: 21, page: url.includes('page=2') ? 2 : 1, page_size: 20 })
      return json({ ...review, issue_count: 21 })
    }))
    const user = userEvent.setup()
    renderApp('/reviews/review-1')
    const first = await screen.findByRole('button', { name: /预算不一致/ })
    expect(first).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('button', { name: /第二个问题/ })).toHaveAttribute('aria-expanded', 'false')
    await user.click(first)
    await user.click(screen.getByRole('button', { name: /第二个问题/ }))
    await act(async () => { await clients[0].invalidateQueries({ queryKey: ['issues'] }) })
    expect(screen.getByRole('button', { name: /预算不一致/ })).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByRole('button', { name: /第二个问题/ })).toHaveAttribute('aria-expanded', 'true')
    await user.click(screen.getByRole('button', { name: '下一页' }))
    expect(await screen.findByRole('button', { name: /第三个问题/ })).toHaveAttribute('aria-expanded', 'false')
    await user.click(screen.getByRole('button', { name: '上一页' }))
    expect(await screen.findByRole('button', { name: /第二个问题/ })).toHaveAttribute('aria-expanded', 'true')
  })

  it.each(['failed', 'cancelled', 'partial'] as const)('does not present an incomplete %s review as an empty success', async status => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url === '/api/v1/session') return json({ ready: true, retention_days: 30 })
      if (url.includes('/issues')) return json({ items: [], total: 0, page: 1, page_size: 20 })
      return json({ ...review, status, issue_count: 0 })
    }))
    renderApp('/reviews/review-1')
    expect(await screen.findByText('暂无审查问题可展示')).toBeInTheDocument()
    expect(screen.queryByText('未发现审查问题')).not.toBeInTheDocument()
    expect(document.querySelector('.successful-empty')).not.toBeInTheDocument()
  })

  it('places JSON and source file details behind disclosures and marks retry outputs as previous results', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url === '/api/v1/session') return json({ ready: true, retention_days: 30 })
      if (url.includes('/issues')) return json({ items: [issue], total: 1, page: 1, page_size: 20 })
      return json({ ...review, status: 'queued', stage: 'queued', artifacts: [...review.artifacts, { id: 'word', name: 'annotated-main.docx', kind: 'docx', media_type: 'application/octet-stream' }, { id: 'json', name: 'report.json', kind: 'json', media_type: 'application/json' }] })
    }))
    const user = userEvent.setup()
    renderApp('/reviews/review-1')
    expect(await screen.findByText('上次结果')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /上次审查问题/ })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '下载 Excel 报告' })).toBeVisible()
    expect(screen.getByRole('link', { name: '下载批注文件' })).toBeVisible()
    expect(screen.getByText('JSON 审查结果')).not.toBeVisible()
    await user.click(screen.getByText('更多下载'))
    expect(screen.getByRole('link', { name: 'JSON 审查结果' })).toHaveAttribute('href', '/api/v1/reviews/review-1/artifacts/json')
    expect(screen.getByText('创建时间')).not.toBeVisible()
    await user.click(screen.getByText('文件与任务信息'))
    expect(screen.getByText('创建时间')).toBeVisible()
  })

  it.each(['queued', 'running', 'failed', 'cancelled'])('hides current issue counts for %s history tasks and keeps identity explanation in About', async status => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url === '/api/v1/session') return json({ ready: true, retention_days: 30 })
      if (url === '/api/v1/config') return json(publicConfig)
      return json({ items: [{ ...review, status, issue_count: 5 }], total: 1, page: 1, page_size: 10 })
    }))
    const user = userEvent.setup()
    renderApp('/history')
    expect(await screen.findByText('—')).toBeInTheDocument()
    expect(screen.queryByText('5 项')).not.toBeInTheDocument()
    expect(screen.getByText(/记录仅对当前浏览器可见/)).not.toBeVisible()
    await user.click(screen.getByText('关于记录'))
    expect(screen.getByText(/记录仅对当前浏览器可见/)).toBeVisible()
  })

  it('exposes only recognized Chinese location labels', () => {
    expect(locationText({ paragraph: 3, xpath: '/w:p', char_start: 4, formula: '=A1', internal: 'secret' })).toBe('段落 3')
    expect(locationText({ internal: 'secret' })).toBe('原文位置')
    expect(locationText({ sheet: '成本', cell: 'B2', row: 2, column: 2 })).toBe('工作表 成本 · 单元格 B2 · 行 2 · 列 2')
  })

  it('keeps optional review requirements collapsed and submits their JSON without losing values on failure', async () => {
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === '/api/v1/session') return json({ ready: true, retention_days: 30 })
      if (url === '/api/v1/config') return json(publicConfig)
      if (init?.method === 'POST') return json({ detail: '请稍后重试' }, 503)
      return json(review)
    })
    vi.stubGlobal('fetch', fetch)
    const user = userEvent.setup()
    renderApp()
    await screen.findByRole('button', { name: '选择文件' })
    expect(screen.getByLabelText('审查要点')).not.toBeVisible()
    await user.click(screen.getByText('补充审查要求'))
    await user.upload(screen.getByLabelText('上传审查文件'), new File(['a'], 'a.pdf'))
    await user.type(screen.getByLabelText('审查要点'), '核对税费\n\n 核对日期 ')
    await user.type(screen.getByLabelText('审查备注'), '项目采用含税口径')
    await user.click(screen.getByRole('button', { name: '开始审查' }))
    expect(await screen.findByText('请稍后重试')).toBeInTheDocument()
    const post = fetch.mock.calls.find(([url]) => url === '/api/v1/reviews')
    const body = post?.[1]?.body as FormData
    expect(JSON.parse(body.get('review_options') as string)).toEqual({ custom_points: ['核对税费', '核对日期'], review_notes: '项目采用含税口径' })
    expect(screen.getByLabelText('审查要点')).toHaveValue('核对税费\n\n 核对日期 ')
    expect(screen.getByLabelText('审查备注')).toHaveValue('项目采用含税口径')
    expect(screen.getByText('a.pdf')).toBeInTheDocument()
  })

  it('reuses submission keys until supplemental requirements change', async () => {
    const fetch = vi.fn(async (url: string, _init?: RequestInit) => {
      if (url === '/api/v1/session') return json({ ready: true, retention_days: 30 })
      if (url === '/api/v1/config') return json(publicConfig)
      return json({ detail: '提交未完成' }, 503)
    })
    vi.stubGlobal('fetch', fetch)
    const user = userEvent.setup()
    renderApp()
    await screen.findByRole('button', { name: '选择文件' })
    await user.upload(screen.getByLabelText('上传审查文件'), new File(['a'], 'a.pdf'))
    await user.click(screen.getByRole('button', { name: '开始审查' }))
    await screen.findByText('提交未完成')
    await user.click(screen.getByRole('button', { name: '开始审查' }))
    await screen.findByText('提交未完成')
    await user.click(screen.getByText('补充审查要求'))
    await user.type(screen.getByLabelText('审查备注'), '核对税费')
    await user.click(screen.getByRole('button', { name: '开始审查' }))
    await screen.findByText('提交未完成')
    const posts = fetch.mock.calls.filter(([url]) => url === '/api/v1/reviews')
    expect(posts).toHaveLength(3)
    const headers = posts.map(([, init]) => init?.headers as Record<string, string>)
    expect(headers[0]['Idempotency-Key']).toBe(headers[1]['Idempotency-Key'])
    expect(headers[2]['Idempotency-Key']).not.toBe(headers[1]['Idempotency-Key'])
  })

  it('validates supplemental point counts, point lengths, and note length before submission', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => json(url === '/api/v1/config' ? publicConfig : { ready: true, retention_days: 30 })))
    const user = userEvent.setup()
    renderApp()
    await screen.findByRole('button', { name: '选择文件' })
    await user.upload(screen.getByLabelText('上传审查文件'), new File(['a'], 'a.pdf'))
    await user.click(screen.getByText('补充审查要求'))
    fireEvent.change(screen.getByLabelText('审查要点'), { target: { value: Array.from({ length: 11 }, (_, index) => `要点${index}`).join('\n') } })
    expect(screen.getByRole('alert')).toHaveTextContent('最多填写 10 项')
    expect(screen.getByRole('button', { name: '开始审查' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('审查要点'), { target: { value: '字'.repeat(501) } })
    expect(screen.getByRole('alert')).toHaveTextContent('每项审查要点不能超过 500 字')
    fireEvent.change(screen.getByLabelText('审查要点'), { target: { value: '核对税费' } })
    fireEvent.change(screen.getByLabelText('审查备注'), { target: { value: '字'.repeat(2001) } })
    expect(screen.getByRole('alert')).toHaveTextContent('审查备注不能超过 2000 字')
    expect(screen.getByRole('button', { name: '开始审查' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('审查备注'), { target: { value: '预算含税' } })
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '开始审查' })).toBeEnabled()
  })

  it('shows actual normalized rules and notes behind the review requirements disclosure', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url === '/api/v1/session') return json({ ready: true, retention_days: 30 })
      if (url.includes('/issues')) return json({ items: [], total: 0, page: 1, page_size: 20 })
      return json({ ...review, review_options: { custom_points: ['原始要求'], review_notes: '原始备注' }, review_requirements: readyRequirements })
    }))
    const user = userEvent.setup()
    renderApp('/reviews/review-1')
    await screen.findByText('审查要求')
    expect(screen.getByText('预算应明确是否包含税费。')).not.toBeVisible()
    await user.click(screen.getByText('审查要求'))
    expect(screen.getByText('预算应明确是否包含税费。')).toBeVisible()
    expect(screen.getByText('预算按含税口径核对。')).toBeVisible()
    expect(screen.getByLabelText('审查要点')).toHaveValue('原始要求')
  })

  it('allows editing rejected requirements and retries with original files and a JSON options body', async () => {
    let current: Review = { ...review, status: 'failed', stage: 'failed', review_options: { custom_points: ['原始要求'], review_notes: '原始备注' }, review_requirements: { ...readyRequirements, status: 'rejected', normalized: null, reasons: ['要求包含无法执行的指令，请改为文件内容核对。'] } }
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === '/api/v1/session') return json({ ready: true, retention_days: 30 })
      if (url.includes('/issues')) return json({ items: [], total: 0, page: 1, page_size: 20 })
      if (url.endsWith('/retry')) current = { ...current, status: 'queued', stage: 'queued', review_requirements: { ...readyRequirements, status: 'pending', normalized: null } }
      return json(current)
    })
    vi.stubGlobal('fetch', fetch)
    const user = userEvent.setup()
    renderApp('/reviews/review-1')
    expect(await screen.findByText('补充审查要求需要调整')).toBeInTheDocument()
    expect(screen.getByText('要求包含无法执行的指令，请改为文件内容核对。')).toBeVisible()
    expect(screen.queryByRole('button', { name: '重试任务' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('审查要点')).toHaveValue('原始要求')
    await user.clear(screen.getByLabelText('审查要点'))
    await user.type(screen.getByLabelText('审查要点'), '核对税费\n核对日期')
    await user.clear(screen.getByLabelText('审查备注'))
    await user.type(screen.getByLabelText('审查备注'), '按含税口径')
    await user.click(screen.getByRole('button', { name: '修改并重试' }))
    expect(await screen.findByText('等待审查资源')).toBeInTheDocument()
    expect(fetch).toHaveBeenCalledWith('/api/v1/reviews/review-1/retry', expect.objectContaining({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ review_options: { custom_points: ['核对税费', '核对日期'], review_notes: '按含税口径' } }) }))
    expect(fetch.mock.calls.some(([url]) => url === '/api/v1/reviews')).toBe(false)
    expect(screen.getByText('上次结果')).toBeInTheDocument()
  })

  it('never exposes editable requirement controls while a review is active', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url === '/api/v1/session') return json({ ready: true, retention_days: 30 })
      if (url.includes('/issues')) return json({ items: [], total: 0, page: 1, page_size: 20 })
      return json({ ...review, status: 'running', review_options: { custom_points: ['核对税费'], review_notes: '' }, review_requirements: { ...readyRequirements, status: 'rejected', normalized: null } })
    }))
    renderApp('/reviews/review-1')
    await screen.findByText('补充审查要求需要调整')
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '修改并重试' })).not.toBeInTheDocument()
    expect(screen.getByText('任务停止后可修改要求并重试。')).toBeInTheDocument()
  })

  it('shows missing custom content without manufacturing absent quotations or locations', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url === '/api/v1/session') return json({ ready: true, retention_days: 30 })
      if (url.includes('/issues')) return json({ items: [{ ...issue, category: 'custom', title: '主文件缺少税费说明', custom_rule_id: 'custom-1', custom_rule_title: '核对税费', finding_kind: 'missing', missing_document_id: 'main', absence_coverage: [{ document_id: 'main', reviewed_span_ids: ['s1'], block_ids: ['b1'], complete: true }], evidence: [issue.evidence[1]] }], total: 1, page: 1, page_size: 20 })
      return json(review)
    }))
    renderApp('/reviews/review-1')
    await screen.findByText('主文件缺少税费说明')
    expect(screen.getByText('专项审查')).toBeInTheDocument()
    expect(screen.getByText('审查要点：核对税费')).toBeInTheDocument()
    expect(screen.getByText('未找到所需内容')).toBeInTheDocument()
    expect(screen.getByText('已核对该文件全部可提取内容。')).toBeInTheDocument()
    expect(screen.getByText('“附件预算120万元”')).toBeInTheDocument()
    expect(screen.queryByText('“未找到所需内容”')).not.toBeInTheDocument()
    expect(screen.queryByText(/段落 3/)).not.toBeInTheDocument()
  })

  it('marks outputs from failed retries as previous results', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url === '/api/v1/session') return json({ ready: true, retention_days: 30 })
      if (url.includes('/issues')) return json({ items: [issue], total: 1, page: 1, page_size: 20 })
      return json({ ...review, status: 'failed' })
    }))
    renderApp('/reviews/review-1')
    expect(await screen.findByText('上次结果')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /上次审查问题/ })).toBeInTheDocument()
  })

  it.each(['completed', 'failed', 'cancelled', 'partial'] as const)('lets %s reviews add requirements from the collapsed area and reuse uploaded files', async status => {
    let current: Review = { ...review, status, review_options: { custom_points: [], review_notes: '' }, review_requirements: { ...readyRequirements, status: 'not_required', normalized: { digest: '', custom_points: [], notes: [] } } }
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === '/api/v1/session') return json({ ready: true, retention_days: 30 })
      if (url.includes('/issues')) return json({ items: [], total: 0, page: 1, page_size: 20 })
      if (url.endsWith('/retry') && init?.method === 'POST') current = { ...current, status: 'queued', stage: 'queued' }
      return json(current)
    })
    vi.stubGlobal('fetch', fetch)
    const user = userEvent.setup()
    renderApp('/reviews/review-1')
    await screen.findByText('审查要求')
    expect(screen.getByLabelText('审查要点')).not.toBeVisible()
    await user.click(screen.getByText('审查要求'))
    expect(screen.getByRole('button', { name: '修改并重试' })).toBeDisabled()
    await user.type(screen.getByLabelText('审查要点'), '核对税费')
    await user.type(screen.getByLabelText('审查备注'), '使用含税口径')
    await user.click(screen.getByRole('button', { name: '修改并重试' }))
    await screen.findByText('等待审查资源')
    expect(fetch).toHaveBeenCalledWith('/api/v1/reviews/review-1/retry', expect.objectContaining({ method: 'POST', body: JSON.stringify({ review_options: { custom_points: ['核对税费'], review_notes: '使用含税口径' } }) }))
    expect(fetch.mock.calls.some(([url]) => url === '/api/v1/reviews')).toBe(false)
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })

  it('keeps malformed requirement drafts editable after a normalization failure and preserves them if retry fails', async () => {
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === '/api/v1/session') return json({ ready: true, retention_days: 30 })
      if (url.includes('/issues')) return json({ items: [], total: 0, page: 1, page_size: 20 })
      if (url.endsWith('/retry') && init?.method === 'POST') return json({ detail: '队列暂时不可用' }, 503)
      return json({ ...review, status: 'failed', review_options: { custom_points: ['原始要点'], review_notes: '原始备注' }, review_requirements: { ...readyRequirements, status: 'failed', normalized: null } })
    })
    vi.stubGlobal('fetch', fetch)
    const user = userEvent.setup()
    renderApp('/reviews/review-1')
    await user.click(await screen.findByText('审查要求'))
    expect(screen.getByLabelText('审查要点')).toHaveValue('原始要点')
    await user.clear(screen.getByLabelText('审查要点'))
    await user.type(screen.getByLabelText('审查要点'), '改为核对税费')
    await user.click(screen.getByRole('button', { name: '修改并重试' }))
    expect(await screen.findByText('队列暂时不可用')).toBeInTheDocument()
    expect(screen.getByLabelText('审查要点')).toHaveValue('改为核对税费')
    expect(screen.getByLabelText('审查备注')).toHaveValue('原始备注')
  })

  it('maps only known pipeline stages and prefixes to trusted progress labels', () => {
    expect(reviewStageName('主文件文字校对与事实提取')).toBe('主文件校对与事实提取')
    expect(reviewStageName('主文件全文数值与语义一致性')).toBe('主文件一致性审查')
    expect(reviewStageName('主文件证据复核')).toBe('主文件证据复核')
    expect(reviewStageName('主文件自定义要点内容搜索')).toBe('核对主文件专项要求')
    expect(reviewStageName('跨文件自定义要点内容搜索')).toBe('核对跨文件专项要求')
    expect(reviewStageName('独立核查自定义内容缺失')).toBe('复核内容缺失')
    expect(reviewStageName('提取其他文件事实：附件.docx')).toBe('提取对照文件事实')
    expect(reviewStageName('与主文件交叉比对：附件.docx')).toBe('跨文件一致性审查')
    expect(reviewStageName('跨文件证据复核：附件.docx')).toBe('跨文件证据复核')
    expect(reviewStageName('主文件自定义要点：用户填写的内容')).toBe('主文件专项审查')
    expect(reviewStageName('跨文件自定义要点：用户填写的内容')).toBe('跨文件专项审查')
    expect(reviewStageName('审查完成，准备导出')).toBe('准备生成审查报告')
    expect(reviewStageName('arbitrary internal stage')).toBe('正在审查')
    expect(reviewStageName('__proto__')).toBe('正在审查')
    expect(reviewStageName('constructor')).toBe('正在审查')
  })

  it('renders a friendly progress label without reflecting a dynamic stage suffix', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url === '/api/v1/session') return json({ ready: true, retention_days: 30 })
      if (url.includes('/issues')) return json({ items: [], total: 0, page: 1, page_size: 20 })
      return json({ ...review, status: 'running', stage: '主文件自定义要点：不可反射的内部字符串', artifacts: [], issue_count: 0 })
    }))
    renderApp('/reviews/review-1')
    expect(await screen.findByRole('heading', { name: '主文件专项审查' })).toBeInTheDocument()
    expect(screen.queryByText(/不可反射的内部字符串/)).not.toBeInTheDocument()
  })
})
