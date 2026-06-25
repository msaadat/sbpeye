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

export type LlmStatusState =
  | 'online'
  | 'rate_limited'
  | 'auth_error'
  | 'not_found'
  | 'offline'
  | 'server_error'
  | 'error'

export interface LlmStatus {
  available: boolean
  state: LlmStatusState
  detail: string
  provider?: string | null
  model?: string | null
  error?: string
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
  source_ref?: string | null
  source_page?: number | null
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

export interface ChecklistSourceUnit {
  unit_id: string
  ref: string
  doc_id: string
  doc_type: 'circular' | 'attachment'
  doc_label: string
  source_text: string
  heading_path: string[]
  page_start?: number | null
  page_end?: number | null
  start_offset: number
  end_offset: number
  oversized: boolean
  kind?: 'text' | 'table' | 'form'
  classification?: 'required' | 'optional' | 'na' | null
  parse_error?: boolean
  evaluation_error?: string | null
}

export interface ChecklistAnalysisBlock {
  block_id: string
  ref: string
  doc_id: string
  doc_type: 'circular' | 'attachment'
  doc_label: string
  block_type: 'regulation' | 'table' | 'form'
  source_unit_ids: string[]
  heading_path: string[]
  page_start?: number | null
  page_end?: number | null
}

export interface ChecklistItem {
  item_id: string
  requirement: string
  classification: 'required' | 'optional'
  actor: string
  action: string
  object: string
  conditions: string
  deadline: string
  evidence: string
  applicability: string
  ref: string
  source_refs: string[]
  source_unit_ids: string[]
  source_text: string
  doc_id: string
  doc_type: 'circular' | 'attachment'
  doc_label: string
  page_start?: number | null
  page_end?: number | null
}

export interface ChecklistCoverageGap {
  doc_id: string
  doc_type: 'circular' | 'attachment'
  doc_label: string
  reason: string
  error?: string | null
}

export interface ComplianceChecklist {
  schema_version: 2
  status: 'completed' | 'completed_with_gaps'
  generated_at: string
  coverage_gaps: ChecklistCoverageGap[]
  checklist_items: ChecklistItem[]
  source_units: ChecklistSourceUnit[]
  analysis_blocks: ChecklistAnalysisBlock[]
}

export type GenerationFeature = 'summary' | 'tags' | 'checklist' | 'relationships'
export type GenerationAction = GenerationFeature | 'all'

export interface CircularGenerationState {
  summary?: string | null
  tags?: string | null
  checklist?: string | null
  relationships?: string | null
}

export interface AIGenerationJob {
  id: string
  circular_id: string
  feature: GenerationAction
  status: 'queued' | 'running' | 'succeeded' | 'failed'
  error?: string | null
  progress_total: number
  progress_completed: number
  result_status?: 'completed' | 'completed_with_gaps' | null
  created_at?: string | null
  started_at?: string | null
  completed_at?: string | null
}

export interface CircularDetail extends CircularSummary {
  attachments: CircularAttachment[]
  attachment_count: number
  compliance_checklist: ComplianceChecklist | null
  relationships: CircularRelationshipsResponse
  generation: CircularGenerationState
}

export interface CircularAttachment {
  id: string
  filename: string
  original_url: string
  file_type?: string | null
  extraction_status?: string | null
  is_scanned: boolean
  is_vectorized: boolean
  has_text: boolean
  local_url: string
}

export interface ResolvedDocument {
  id: string
  circular_id: string
  filename: string
  file_type?: string | null
  original_url: string
  cached: boolean
  content_url: string
  extraction_status?: string | null
  error?: string | null
}

export interface CircularSourceContent {
  type: 'html' | 'pdf'
  url: string
  content?: string | null
  preview_url?: string | null
  original_url?: string | null
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
  api_key_configured?: boolean
  api_key_env_var?: string
  clear_api_key?: boolean
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
  embedding_api_key_configured?: boolean
  embedding_api_key_env_var?: string
  clear_embedding_api_key?: boolean
  managed_env_file?: string
}

export interface ChatSession {
  id: string
  title?: string | null
  session_type?: 'chat' | 'workspace'
  workspace_id?: string | null
  is_default_workspace?: boolean
  pinned_count?: number
  circular_ids?: string[]
  created_at?: string | null
  updated_at?: string | null
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant' | string
  content: string
  circular_ids?: string[]
  created_at?: string | null
}

export interface ChatSessionDetail {
  id: string
  title?: string | null
  session_type?: 'chat' | 'workspace'
  workspace_id?: string | null
  is_default_workspace?: boolean
  pinned_count?: number
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

export interface ResearchWorkspace {
  id: string
  name: string
  is_default?: boolean
  search_state: SearchFilters
  last_circular_id?: string | null
  pinned_circular_ids: string[]
  pinned_circulars: CircularSummary[]
  pinned_count: number
  created_at?: string | null
  updated_at?: string | null
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

export async function getLlmStatus(): Promise<LlmStatus> {
  return requestJson<LlmStatus>('/llm/status')
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

export async function startCircularGeneration(id: string, feature: GenerationAction): Promise<AIGenerationJob> {
  return requestJson<AIGenerationJob>(`/circulars/${encodeURIComponent(id)}/generate`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ feature }),
  })
}

export async function getAIGenerationJob(jobId: string): Promise<AIGenerationJob> {
  return requestJson<AIGenerationJob>(`/ai/jobs/${encodeURIComponent(jobId)}`)
}

export async function getCircularSource(id: string): Promise<CircularSourceContent> {
  return requestJson<CircularSourceContent>(`/circulars/${encodeURIComponent(id)}/source`)
}

export async function getCircularByUrl(url: string): Promise<CircularSummary> {
  return requestJson<CircularSummary>(`/circulars/by_url${toQueryString({ url })}`)
}

export async function openCircularByUrl(url: string): Promise<CircularSummary> {
  return requestJson<CircularSummary>(`/circulars/open${toQueryString({ url })}`, { method: 'POST' })
}

export async function refreshCircular(id: string): Promise<CircularSummary> {
  return requestJson<CircularSummary>(`/circulars/${encodeURIComponent(id)}/refresh`, { method: 'POST' })
}

export async function resolveDocument(
  params: { id?: string; url?: string; circular_id?: string },
  refresh = false,
): Promise<ResolvedDocument> {
  return requestJson<ResolvedDocument>(`/documents/resolve${toQueryString({ ...params, refresh })}`, { method: 'POST' })
}

export function buildDocumentContentUrl(id: string): string {
  return `${API_BASE}/documents/${encodeURIComponent(id)}/content`
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

export async function getResearchWorkspaces(): Promise<ResearchWorkspace[]> {
  return requestJson<ResearchWorkspace[]>('/workspaces')
}

export async function createResearchWorkspace(payload: {
  name: string
  search_state?: SearchFilters
  last_circular_id?: string | null
}): Promise<ResearchWorkspace> {
  return requestJson<ResearchWorkspace>('/workspaces', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

export async function getResearchWorkspace(id: string): Promise<ResearchWorkspace> {
  return requestJson<ResearchWorkspace>(`/workspaces/${encodeURIComponent(id)}`)
}

export async function updateResearchWorkspace(
  id: string,
  payload: {
    name?: string
    search_state?: SearchFilters
    last_circular_id?: string | null
  },
): Promise<ResearchWorkspace> {
  return requestJson<ResearchWorkspace>(`/workspaces/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

export async function deleteResearchWorkspace(id: string): Promise<{ success: boolean }> {
  return requestJson<{ success: boolean }>(`/workspaces/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  })
}

export async function pinWorkspaceCircular(
  workspaceId: string,
  circularId: string,
): Promise<ResearchWorkspace> {
  return requestJson<ResearchWorkspace>(`/workspaces/${encodeURIComponent(workspaceId)}/circulars`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ circular_id: circularId }),
  })
}

export async function unpinWorkspaceCircular(
  workspaceId: string,
  circularId: string,
): Promise<ResearchWorkspace> {
  return requestJson<ResearchWorkspace>(
    `/workspaces/${encodeURIComponent(workspaceId)}/circulars/${encodeURIComponent(circularId)}`,
    { method: 'DELETE' },
  )
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

export async function saveSettings(payload: SettingsPayload): Promise<{ message: string; settings: SettingsPayload; context_window_detected: boolean }> {
  return requestJson<{ message: string; settings: SettingsPayload; context_window_detected: boolean }>('/settings', {
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

export async function renameChatSession(
  sessionId: string,
  title: string,
): Promise<ChatSession> {
  return requestJson<ChatSession>(`/chat/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  })
}

export async function truncateChatSession(
  sessionId: string,
  messageId: string,
): Promise<{ success: boolean }> {
  return requestJson<{ success: boolean }>(
    `/chat/sessions/${encodeURIComponent(sessionId)}/messages/${encodeURIComponent(messageId)}`,
    { method: 'DELETE' },
  )
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
    replace_message_id?: string
  },
  handlers: {
    onSession?: (sessionId: string) => void
    onToken: (content: string) => void
    onError?: (message: string) => void
    onDone?: (sessionId?: string) => void
  },
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${API_BASE}/chat/stream`, {
    method: 'POST',
    headers: {
      Accept: 'text/event-stream',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(payload),
    signal,
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

export async function downloadChecklistExcel(id: string, reference?: string | null): Promise<void> {
  const response = await fetch(`${API_BASE}/circulars/${encodeURIComponent(id)}/checklist.xlsx`)

  if (!response.ok) {
    const payload = await readErrorPayload(response)
    throw Object.assign(new Error(payload?.error || `Checklist export failed with ${response.status}`), {
      status: response.status,
      payload,
    }) as ApiError
  }

  const blob = await response.blob()
  const downloadUrl = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = downloadUrl
  const safeReference = (reference || id).replace(/[^A-Za-z0-9._-]+/g, '_').replace(/^[._]+|[._]+$/g, '')
  anchor.download = `${safeReference || 'circular'}_checklist.xlsx`
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
