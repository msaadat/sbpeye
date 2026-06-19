export interface ApiErrorPayload {
  error?: string
  message?: string
  detail?: string
  [key: string]: unknown
}

export interface ApiError extends Error {
  status: number
  payload?: ApiErrorPayload
}

export interface SbpNewsItem {
  title: string
  url: string
}

export interface SbpNewsResponse {
  press_releases: SbpNewsItem[]
  whats_new: SbpNewsItem[]
  error?: string
}

export interface AppStatus {
  sync_status?: string
  live_status?: string
  total_circulars?: number
  department_count?: number
  indexed_today?: number
  vector_db_state?: string
  last_sync_display?: string
  last_sync?: string
  last_sync_dt?: string | null
  last_sync_raw?: string | null
}

export interface PaginatedResponse<T> {
  items: T[]
  total: number
  page: number
  per_page: number
  total_pages: number
}

export interface CircularSummary {
  id: string
  title: string
  department?: string | null
  reference?: string | null
  date?: string | null
  url?: string | null
  summary?: string | null
  tags: string[]
  status: string
  snippet: string
}

export interface CircularRelationshipTarget {
  id: string
  title?: string | null
  reference?: string | null
  url?: string | null
  status?: string | null
}

export interface CircularRelationship {
  type: string
  source_id?: string | null
  target_id?: string | null
  target_reference?: string | null
  confidence?: number | null
  source?: CircularRelationshipTarget | null
  target?: CircularRelationshipTarget | null
}

export interface CircularRelationshipsResponse {
  outgoing: CircularRelationship[]
  incoming: CircularRelationship[]
}

export interface CircularDetail extends CircularSummary {
  compliance_checklist: string[]
  relationships: CircularRelationshipsResponse
}

export interface CircularSourceContent {
  type: 'html' | 'pdf'
  url: string
  content?: string | null
  preview_url?: string | null
  error?: string
}

export interface TagCount {
  tag: string
  count: number
}

export interface DepartmentCount {
  department: string | null
  count: number
}

export interface YearCount {
  year: number
  count: number
}

export interface EcoDataEntry {
  id: string
  section: string
  subsection?: string | null
  description: string
  url?: string | null
  frequency?: string | null
  format_url?: string | null
  format_type?: string | null
  last_update?: string | null
  archive_url?: string | null
  archive_updated?: string | null
  is_quick_link: boolean
  can_summarize: boolean
}

export interface SettingsPayload {
  provider: string
  base_url: string
  api_key: string
  model: string
  chat_model: string
  max_context_tokens: number
  ai_provider?: string
  ai_base_url?: string
  ai_api_key?: string
  ai_model?: string
  ai_chat_model?: string
  ai_max_context_tokens?: number
  embedding_provider: string
  embedding_model: string
  embedding_base_url: string
  embedding_api_key: string
}

export interface ChatSession {
  id: string
  title?: string | null
  created_at?: string | null
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant' | string
  content: string
  created_at?: string | null
}

export interface ChatSessionDetail {
  id: string
  title?: string | null
  messages: ChatMessage[]
  circulars: CircularSummary[]
}

export interface ChatResponse {
  response: string
  session_id: string
}

export interface PdfPreviewResponse {
  type?: 'text' | 'image'
  content?: string | null
  pages?: number
  error?: string
}

export interface SearchFilters {
  q?: string
  start_year?: string | number | null
  end_year?: string | number | null
  department?: string | null
  sort_by?: string
  tag?: string | null
  page?: number
  per_page?: number
}

const API_BASE = '/api'

function toQueryString(params: Record<string, string | number | boolean | null | undefined>): string {
  const search = new URLSearchParams()

  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === '') {
      continue
    }

    search.set(key, String(value))
  }

  const query = search.toString()
  return query ? `?${query}` : ''
}

async function readErrorPayload(response: Response): Promise<ApiErrorPayload | undefined> {
  const contentType = response.headers.get('content-type') || ''

  if (!contentType.includes('application/json')) {
    const text = await response.text().catch(() => '')
    return text ? { error: text } : undefined
  }

  try {
    return (await response.json()) as ApiErrorPayload
  } catch {
    return undefined
  }
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init?.headers || {}),
    },
  })

  if (!response.ok) {
    const payload = await readErrorPayload(response)
    const error = new Error(payload?.error || payload?.message || `Request failed with ${response.status}`) as ApiError
    error.status = response.status
    error.payload = payload
    throw error
  }

  return (await response.json()) as T
}

export async function getAppStatus(): Promise<AppStatus> {
  return requestJson<AppStatus>('/app/status')
}

export async function getSbpNews(): Promise<SbpNewsResponse> {
  return requestJson<SbpNewsResponse>('/sbp_news')
}

export async function getCircularSearch(
  filters: SearchFilters = {},
  signal?: AbortSignal,
): Promise<PaginatedResponse<CircularSummary>> {
  const {
    q = '',
    start_year,
    end_year,
    department,
    sort_by = 'relevance',
    tag,
    page = 1,
    per_page = 20,
  } = filters

  return requestJson<PaginatedResponse<CircularSummary>>(
    `/circulars/search${toQueryString({ q, start_year, end_year, department, sort_by, tag, page, per_page })}`,
    { signal },
  )
}

export async function getCircularDetail(id: string): Promise<CircularDetail> {
  return requestJson<CircularDetail>(`/circulars/${encodeURIComponent(id)}`)
}

export async function getCircularSource(id: string): Promise<CircularSourceContent> {
  return requestJson<CircularSourceContent>(`/circulars/${encodeURIComponent(id)}/source`)
}

export async function getCircularByUrl(url: string): Promise<CircularSummary> {
  return requestJson<CircularSummary>(`/circulars/by_url${toQueryString({ url })}`)
}

export async function getCircularTags(): Promise<TagCount[]> {
  return requestJson<TagCount[]>('/circulars/tags')
}

export async function getCircularDepartments(): Promise<DepartmentCount[]> {
  return requestJson<DepartmentCount[]>('/circulars/departments')
}

export async function getCircularYears(department: string): Promise<YearCount[]> {
  return requestJson<YearCount[]>(`/circulars/years${toQueryString({ department })}`)
}

export async function getCircularBrowse(department: string, year: number): Promise<CircularSummary[]> {
  return requestJson<CircularSummary[]>(`/circulars/browse${toQueryString({ department, year })}`)
}

export async function getCircularBrowseRecent(limit = 100): Promise<CircularSummary[]> {
  return requestJson<CircularSummary[]>(`/circulars/browse_recent${toQueryString({ limit })}`)
}

export async function getEcoData(series = 'KIBOR_6M'): Promise<Array<{ date: string; value: number }>> {
  return requestJson<Array<{ date: string; value: number }>>(`/ecodata${toQueryString({ series })}`)
}

export async function getEcoDataEntries(): Promise<EcoDataEntry[]> {
  return requestJson<EcoDataEntry[]>('/ecodata/entries')
}

export interface EcoDataPdfSummaryResponse {
  summary?: string
  url: string
  error?: string
}

export async function getEcoDataPdfSummary(url: string): Promise<EcoDataPdfSummaryResponse> {
  return requestJson<EcoDataPdfSummaryResponse>(`/ecodata/pdf_summary${toQueryString({ url })}`)
}

export async function getPdfPreview(url: string): Promise<PdfPreviewResponse> {
  return requestJson<PdfPreviewResponse>(`/pdf_preview${toQueryString({ url })}`)
}

export function buildPdfProxyUrl(url: string): string {
  return `${API_BASE}/pdf_proxy${toQueryString({ url })}`
}

export async function getSettings(): Promise<SettingsPayload> {
  return requestJson<SettingsPayload>('/settings')
}

export async function saveSettings(payload: SettingsPayload): Promise<{ message: string; settings: SettingsPayload }> {
  return requestJson<{ message: string; settings: SettingsPayload }>('/settings', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(payload),
  })
}

export async function testSettingsConnection(): Promise<Record<string, unknown>> {
  return requestJson<Record<string, unknown>>('/settings/test', {
    method: 'POST',
  })
}

export async function testEmbeddingConnection(): Promise<Record<string, unknown>> {
  return requestJson<Record<string, unknown>>('/settings/embeddings/test', {
    method: 'POST',
  })
}

export async function getChatSessions(): Promise<ChatSession[]> {
  return requestJson<ChatSession[]>('/chat/sessions')
}

export async function getChatSession(sessionId: string): Promise<ChatSessionDetail> {
  return requestJson<ChatSessionDetail>(`/chat/sessions/${encodeURIComponent(sessionId)}`)
}

export async function deleteChatSession(sessionId: string): Promise<{ success: boolean }> {
  return requestJson<{ success: boolean }>(`/chat/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
  })
}

export async function sendChatMessage(payload: {
  message: string
  session_id?: string | null
  circular_ids?: string[]
}): Promise<ChatResponse> {
  return requestJson<ChatResponse>('/chat', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(payload),
  })
}

export async function streamChatMessage(
  payload: {
    message: string
    session_id?: string | null
    circular_ids?: string[]
  },
  handlers: {
    onSession?: (sessionId: string) => void
    onToken: (content: string) => void
    onError?: (message: string) => void
    onDone?: (sessionId?: string) => void
  },
): Promise<void> {
  const response = await fetch(`${API_BASE}/chat/stream`, {
    method: 'POST',
    headers: {
      Accept: 'text/event-stream',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(payload),
  })

  if (!response.ok) {
    const errorPayload = await readErrorPayload(response)
    throw Object.assign(new Error(errorPayload?.error || `Chat request failed with ${response.status}`), {
      status: response.status,
      payload: errorPayload,
    }) as ApiError
  }

  if (!response.body) {
    throw new Error('Streaming is unavailable in this browser.')
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  function handleEvent(rawEvent: string) {
    let event = 'message'
    const dataLines: string[] = []

    for (const line of rawEvent.split('\n')) {
      if (line.startsWith('event:')) {
        event = line.slice(6).trim()
      } else if (line.startsWith('data:')) {
        dataLines.push(line.slice(5).trimStart())
      }
    }

    if (!dataLines.length) {
      return
    }

    const data = JSON.parse(dataLines.join('\n')) as { content?: string; session_id?: string; error?: string }

    if (event === 'meta' && data.session_id) {
      handlers.onSession?.(data.session_id)
      return
    }

    if (event === 'token' && data.content) {
      handlers.onToken(data.content)
      return
    }

    if (event === 'done') {
      handlers.onDone?.(data.session_id)
      return
    }

    if (event === 'error') {
      handlers.onError?.(data.error || 'Chat stream failed.')
    }
  }

  while (true) {
    const { value, done } = await reader.read()
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done })

    let boundary = buffer.indexOf('\n\n')
    while (boundary >= 0) {
      const rawEvent = buffer.slice(0, boundary).trim()
      buffer = buffer.slice(boundary + 2)
      if (rawEvent) {
        handleEvent(rawEvent)
      }
      boundary = buffer.indexOf('\n\n')
    }

    if (done) {
      if (buffer.trim()) {
        handleEvent(buffer.trim())
      }
      break
    }
  }
}

export function buildCsvExportUrl(filters: SearchFilters = {}): string {
  const {
    q = '',
    start_year,
    end_year,
    department,
    sort_by = 'relevance',
    tag,
  } = filters

  return `${API_BASE}/circulars/export_csv${toQueryString({ q, start_year, end_year, department, sort_by, tag })}`
}

export function navigateToCsvExport(filters: SearchFilters = {}): void {
  window.location.assign(buildCsvExportUrl(filters))
}

export async function downloadCsvExport(filters: SearchFilters = {}): Promise<void> {
  const response = await fetch(buildCsvExportUrl(filters))

  if (!response.ok) {
    const payload = await readErrorPayload(response)
    throw Object.assign(new Error(payload?.error || `CSV export failed with ${response.status}`), {
      status: response.status,
      payload,
    }) as ApiError
  }

  const blob = await response.blob()
  const downloadUrl = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = downloadUrl
  anchor.download = 'sbpeye_search_results.csv'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(downloadUrl)
}

export async function downloadBatchZip(circularIds: string[]): Promise<void> {
  const formData = new FormData()
  for (const id of circularIds) {
    formData.append('circular_ids', id)
  }

  const response = await fetch(`${API_BASE}/circulars/batch_download`, {
    method: 'POST',
    body: formData,
  })

  if (!response.ok) {
    const payload = await readErrorPayload(response)
    throw Object.assign(new Error(payload?.error || `Batch download failed with ${response.status}`), {
      status: response.status,
      payload,
    }) as ApiError
  }

  const blob = await response.blob()
  const downloadUrl = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = downloadUrl
  anchor.download = 'circulars_batch.zip'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(downloadUrl)
}
