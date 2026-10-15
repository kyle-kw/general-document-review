export type ReviewStatus = 'queued' | 'running' | 'completed' | 'partial' | 'failed' | 'cancelled'

export interface SourceFile {
  id: string
  filename: string
  is_main: boolean
  size: number
}

export interface Warning {
  code: string
  message: string
  location?: Record<string, unknown>
  document_id?: string
  filename?: string
  incomplete?: boolean
}

export interface Artifact {
  id: string
  name: string
  kind: string
  media_type: string
}

export interface ReviewOptions {
  custom_points: string[]
  review_notes: string
}

export interface NormalizedRule {
  id: string
  title: string
  criterion: string
  kind: 'required_content' | 'content_constraint' | 'consistency'
}

export interface RequirementsState {
  status: 'not_required' | 'pending' | 'rewriting' | 'validating' | 'ready' | 'rejected' | 'failed'
  raw_digest: string
  policy_version: string
  reasons: string[]
  normalized: { notes: string[], custom_points: NormalizedRule[], digest: string } | null
}

export interface Review {
  id: string
  title: string
  status: ReviewStatus
  cancel_requested: boolean
  stage: string
  progress_current: number
  progress_total: number
  issue_count: number
  created_at: string
  finished_at: string | null
  expires_at: string | null
  error: string | null
  files: SourceFile[]
  warnings: Warning[]
  artifacts: Artifact[]
  review_options?: ReviewOptions
  review_requirements?: RequirementsState
}

export interface Evidence {
  document_id: string
  block_id: string
  quote: string
  start?: number | null
  end?: number | null
  location: Record<string, unknown>
  purpose?: 'quote' | 'context'
}

export interface Issue {
  id: string
  category: 'typo' | 'punctuation' | 'numeric' | 'semantic' | 'custom'
  scope: 'main' | 'cross'
  severity: 'high' | 'medium' | 'low'
  confidence: 'confirmed' | 'needs_confirmation'
  title: string
  description: string
  suggestion: string
  evidence: Evidence[]
  annotation_status: string
  annotation_reason: string
  custom_rule_id?: string | null
  custom_rule_title?: string
  finding_kind?: 'violation' | 'missing'
  missing_document_id?: string | null
  absence_coverage?: { document_id: string, reviewed_span_ids: string[], block_ids: string[], complete: boolean, passes?: number }[]
}

export interface Page<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

export interface PublicConfig {
  max_files: number
  max_file_mb: number
  retention_days: number
  ocr_enabled: boolean
  llm_configured: boolean
}

const prefix = '/api/v1'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${prefix}${path}`, { ...init, credentials: 'same-origin' })
  } catch {
    throw new Error('无法连接服务，请检查网络后重试。')
  }
  if (!response.ok) {
    let message = `请求失败（${response.status}），请稍后重试。`
    try {
      const body: { detail?: unknown } = await response.json()
      if (typeof body.detail === 'string') message = body.detail
      else if (response.status === 422) message = '文件或请求格式不符合要求，请检查后重试。'
    } catch { /* An upstream service may return a non-JSON error. */ }
    throw new Error(message)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const api = {
  session: () => request<{ ready: boolean, retention_days: number }>('/session', { method: 'POST' }),
  config: () => request<PublicConfig>('/config'),
  reviews: (page: number) => request<Page<Review>>(`/reviews?page=${page}&page_size=10`),
  review: (id: string) => request<Review>(`/reviews/${encodeURIComponent(id)}`),
  issues: (id: string, page: number) => request<Page<Issue>>(`/reviews/${encodeURIComponent(id)}/issues?page=${page}&page_size=20`),
  create: (files: File[], main: number, key: string, options: ReviewOptions = { custom_points: [], review_notes: '' }) => {
    const body = new FormData()
    body.append('main_file', files[main])
    files.forEach((file, index) => { if (index !== main) body.append('reference_files', file) })
    body.append('review_options', JSON.stringify(options))
    return request<{ id: string, status: ReviewStatus }>('/reviews', { method: 'POST', body, headers: { 'Idempotency-Key': key } })
  },
  action: (id: string, action: 'cancel' | 'retry', options?: ReviewOptions) => request<Review>(`/reviews/${encodeURIComponent(id)}/${action}`, options && action === 'retry' ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ review_options: options }) } : { method: 'POST' }),
  remove: (id: string) => request<void>(`/reviews/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  download: (id: string, type: 'files' | 'artifacts', fileId: string) => `${prefix}/reviews/${encodeURIComponent(id)}/${type}/${encodeURIComponent(fileId)}`,
}

export const isActive = (status: ReviewStatus) => status === 'queued' || status === 'running'

// getRandomValues also works on offline LAN deployments served over plain HTTP.
export const newSubmissionKey = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), value => value.toString(16).padStart(2, '0')).join('')
