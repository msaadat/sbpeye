<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import ProgressSpinner from 'primevue/progressspinner'
import Textarea from 'primevue/textarea'
import CircularResultContent from '@/components/CircularResultContent.vue'
import {
  buildDocumentContentUrl,
  deleteChatSession,
  getChatSession,
  getChatSessions,
  getCircularDetail,
  getCircularSearch,
  renameChatSession,
  resolveDocument,
  streamChatMessage,
  truncateChatSession,
  type ChatMessage,
  type ChatSession,
  type CircularSummary,
  type ResolvedDocument,
} from '@/lib/api'

const PdfPreviewDialog = defineAsyncComponent(() => import('@/components/PdfPreviewDialog.vue'))

interface LocalMessage extends ChatMessage {
  pending?: boolean
}

interface TurnStep {
  tools: string[]
  note: string
}

const route = useRoute()
const router = useRouter()
const confirm = useConfirm()
const toast = useToast()

const sessions = ref<ChatSession[]>([])
const messages = ref<LocalMessage[]>([])
const selectedCirculars = ref<CircularSummary[]>([])
const searchResults = ref<CircularSummary[]>([])
const currentSessionId = ref<string | null>(null)
const inputMessage = ref('')
const contextQuery = ref('')
const sessionFilter = ref('')
const sessionsLoading = ref(false)
const sessionLoading = ref(false)
const searchLoading = ref(false)
const sending = ref(false)
const editingMessageId = ref<string | null>(null)
const editDraft = ref('')
const renamingSessionId = ref<string | null>(null)
const renameDraft = ref('')
const errorMessage = ref('')
const stoppedNotice = ref(false)
const railOpen = ref(false)
const messagesEl = ref<HTMLElement | null>(null)
const composerShellEl = ref<HTMLElement | null>(null)
const pinnedToBottom = ref(true)
const attachmentDialogVisible = ref(false)
const selectedAttachment = ref<ResolvedDocument | null>(null)
const circularCitationCache = ref<Record<string, CircularSummary | null>>({})

// Live progress for the turn in flight.
const activityLabel = ref('')
const turnSteps = ref<TurnStep[]>([])
const turnStepsSessionId = ref<string | null>(null)
const elapsedSeconds = ref(0)
let elapsedTimer: number | undefined

let searchTimer: number | undefined
let streamController: AbortController | null = null
const circularCitationLoads = new Set<string>()
const uuidPattern = /\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b/gi
// The id segment accepts anything that is not a delimiter: attachments are cited
// by filename, so restricting it to [a-zA-Z0-9-] left tokens like
// [[attachment:c2-AML-CFT-Regulations.pdf|…]] unmatched and printed raw.
const citationTokenPattern = /\[{1,2}\s*(circular|attachment|law)\s*:\s*([^|\]\s]+)\s*\|\s*([^\]\n]+?)\s*\]{1,2}/gi
const blockCitationPattern = /\[\[\s*(circular|attachment|law)\s*:\s*([^|\]\s]+)\s*\|\s*([\s\S]*?)\s*\]\]/g

marked.use({
  breaks: true,
  gfm: true,
})

const hasContext = computed(() => selectedCirculars.value.length > 0)
const activeSession = computed(() => sessions.value.find((session) => session.id === currentSessionId.value) || null)
const activeSessionIsWorkspace = computed(() => activeSession.value?.session_type === 'workspace')
const contextModeLabel = computed(() => {
  if (activeSessionIsWorkspace.value) return `${selectedCirculars.value.length} workspace pins`
  return hasContext.value ? `${selectedCirculars.value.length} attached` : 'Whole corpus'
})
const contextModeHint = computed(() =>
  hasContext.value
    ? 'Answers are restricted to the attached documents first, then the wider index.'
    : 'Answers search the full circular index. Attach documents to narrow the scope.',
)
const elapsedLabel = computed(() => {
  const total = elapsedSeconds.value
  if (total < 60) return `${total}s`
  return `${Math.floor(total / 60)}m ${String(total % 60).padStart(2, '0')}s`
})
const lastUserMessage = computed(() => [...messages.value].reverse().find((message) => message.role === 'user') || null)

function formatDate(value?: string | null): string {
  if (!value) {
    return ''
  }

  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: '2-digit',
    year: 'numeric',
  }).format(new Date(value))
}

function sessionTimestamp(session: ChatSession): Date {
  return new Date(session.updated_at || session.created_at || Date.now())
}

/** Same-day sessions are told apart by time; older ones by date. */
function sessionDate(session: ChatSession): string {
  const stamp = sessionTimestamp(session)
  const startOfToday = new Date()
  startOfToday.setHours(0, 0, 0, 0)
  if (stamp >= startOfToday) {
    return new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' }).format(stamp)
  }
  return formatDate(session.updated_at || session.created_at)
}

function isWorkspaceSession(session: ChatSession | null | undefined): boolean {
  return session?.session_type === 'workspace'
}

function sessionIconClass(session: ChatSession): string {
  if (!isWorkspaceSession(session)) return 'pi-comments'
  return session.is_default_workspace ? 'pi-home' : 'pi-folder'
}

function chatGroupLabel(session: ChatSession): string {
  const stamp = sessionTimestamp(session).getTime()
  const startOfToday = new Date()
  startOfToday.setHours(0, 0, 0, 0)
  const day = 24 * 60 * 60 * 1000
  if (stamp >= startOfToday.getTime()) return 'Today'
  if (stamp >= startOfToday.getTime() - day) return 'Yesterday'
  if (stamp >= startOfToday.getTime() - 7 * day) return 'Previous 7 days'
  return 'Older'
}

/** Workspaces first, then chats bucketed by recency — the rail was one flat list
    where a workspace and a week-old thread looked identical. */
const sessionGroups = computed(() => {
  const needle = sessionFilter.value.trim().toLowerCase()
  const visible = needle
    ? sessions.value.filter((session) => (session.title || 'New chat').toLowerCase().includes(needle))
    : sessions.value

  const groups: { label: string; sessions: ChatSession[] }[] = []
  const push = (label: string, session: ChatSession) => {
    const existing = groups.find((group) => group.label === label)
    if (existing) existing.sessions.push(session)
    else groups.push({ label, sessions: [session] })
  }

  for (const session of visible) {
    push(isWorkspaceSession(session) ? 'Workspaces' : chatGroupLabel(session), session)
  }
  return groups
})

const visibleSessionCount = computed(() =>
  sessionGroups.value.reduce((total, group) => total + group.sessions.length, 0),
)

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (character) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[character] || character))
}

function cacheCircularCitation(circular: CircularSummary) {
  circularCitationCache.value = {
    ...circularCitationCache.value,
    [circular.id.toLowerCase()]: circular,
  }
}

function circularCitationLabel(id: string): string {
  const circular = circularCitationCache.value[id.toLowerCase()]
  return circular?.reference || circular?.title || 'Circular'
}

function circularCitationHtml(id: string, label = circularCitationLabel(id)): string {
  const href = `/circulars/${encodeURIComponent(id)}`
  return `<a href="${href}" class="document-pill chat-citation-pill" data-document-link="true"><i class="pi pi-file"></i><span>${escapeHtml(label)}</span></a>`
}

function citationHtml(kind: string, id: string, label: string): string {
  if (kind === 'circular') {
    return circularCitationHtml(id, label.trim() || circularCitationLabel(id))
  }

  // A regulation opens in the laws reader. Unlike a circular there is no id-to-label
  // cache to fall back on, so an unlabelled token degrades to the corpus's own word.
  if (kind === 'law') {
    const href = `/laws/${encodeURIComponent(id)}`
    return `<a href="${href}" class="document-pill chat-citation-pill" data-document-link="true"><i class="pi pi-book"></i><span>${escapeHtml(label.trim() || 'Regulation')}</span></a>`
  }

  const href = `/documents/open?id=${encodeURIComponent(id)}`
  return `<a href="${href}" class="document-pill chat-citation-pill" data-document-link="true"><i class="pi pi-paperclip"></i><span>${escapeHtml(label.trim() || 'Attachment')}</span></a>`
}

function normalizeCitationTokens(content: string): string {
  return content
    .replace(
      blockCitationPattern,
      (_match, kind: string, id: string, label: string) => citationHtml(kind, id, label),
    )
    .replace(
      citationTokenPattern,
      (_match, kind: string, id: string, label: string) => citationHtml(kind, id, label),
    )
}

/** Plain-text form for the clipboard and file export: citation tokens collapse to
    their human label so a pasted answer does not carry raw markup. */
function messageToPlainText(content: string): string {
  return content
    .replace(blockCitationPattern, (_match, _kind: string, _id: string, label: string) => label.trim())
    .replace(citationTokenPattern, (_match, _kind: string, _id: string, label: string) => label.trim())
    .replace(uuidPattern, (id) => circularCitationLabel(id))
}

function collectCircularIds(content: string): string[] {
  // Tokens that already carry a label need no lookup — resolving them was the
  // bulk of the request storm (and of the 404s for non-circular ids).
  const stripped = content.replace(blockCitationPattern, ' ').replace(citationTokenPattern, ' ')
  const ids = new Set<string>()
  for (const match of stripped.matchAll(uuidPattern)) {
    ids.add(match[0].toLowerCase())
  }
  return [...ids]
}

async function loadCircularCitation(id: string) {
  const normalized = id.toLowerCase()
  if (normalized in circularCitationCache.value || circularCitationLoads.has(normalized)) {
    return
  }

  circularCitationLoads.add(normalized)
  try {
    const circular = await getCircularDetail(normalized)
    cacheCircularCitation(circular)
  } catch {
    circularCitationCache.value = {
      ...circularCitationCache.value,
      [normalized]: null,
    }
  } finally {
    circularCitationLoads.delete(normalized)
  }
}

function preloadCircularCitations() {
  for (const message of messages.value) {
    if (message.role !== 'assistant') continue
    for (const id of collectCircularIds(message.content)) {
      void loadCircularCitation(id)
    }
  }
}

function replaceBareCircularIds(root: DocumentFragment) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  const replacements: Text[] = []

  while (walker.nextNode()) {
    const node = walker.currentNode
    if (!(node instanceof Text) || !node.nodeValue) {
      continue
    }

    const hasCitation = citationTokenPattern.test(node.nodeValue)
    const hasUuid = uuidPattern.test(node.nodeValue)
    citationTokenPattern.lastIndex = 0
    uuidPattern.lastIndex = 0
    if (!hasCitation && !hasUuid) {
      uuidPattern.lastIndex = 0
      continue
    }

    const parent = node.parentElement
    if (parent?.closest('a, code, pre')) {
      uuidPattern.lastIndex = 0
      continue
    }

    replacements.push(node)
    uuidPattern.lastIndex = 0
  }

  for (const node of replacements) {
    const text = node.nodeValue || ''
    const fragment = document.createDocumentFragment()
    let lastIndex = 0
    const pattern = citationTokenPattern.test(text) ? citationTokenPattern : uuidPattern
    citationTokenPattern.lastIndex = 0
    pattern.lastIndex = 0

    for (const match of text.matchAll(pattern)) {
      const kind = match[1]
      const id = pattern === citationTokenPattern ? match[2] : match[0]
      const label = pattern === citationTokenPattern ? match[3] : undefined
      const index = match.index || 0
      if (index > lastIndex) {
        fragment.append(document.createTextNode(text.slice(lastIndex, index)))
      }

      const template = document.createElement('template')
      template.innerHTML = pattern === citationTokenPattern
        ? citationHtml(kind, id, label || '')
        : circularCitationHtml(id)
      fragment.append(template.content)
      lastIndex = index + match[0].length
    }

    if (lastIndex < text.length) {
      fragment.append(document.createTextNode(text.slice(lastIndex)))
    }

    node.replaceWith(fragment)
    pattern.lastIndex = 0
  }
}

// Every streamed token replaces the messages array, which re-evaluates this for
// every message in the thread. Parsing a 12k-character answer through marked +
// DOMPurify + a tree walk on each token is the difference between a smooth
// stream and a stuttering one, so completed messages are rendered once.
const markdownCache = new Map<string, string>()
const citationVersion = ref(0)

function renderMarkdown(content: string): string {
  const key = `${citationVersion.value} ${content}`
  const cached = markdownCache.get(key)
  if (cached !== undefined) {
    return cached
  }

  const withCitations = normalizeCitationTokens(content)
  const sanitized = DOMPurify.sanitize(marked.parse(withCitations) as string, {
    USE_PROFILES: { html: true },
  })
  const template = document.createElement('template')
  template.innerHTML = sanitized
  template.content.querySelectorAll<HTMLAnchorElement>('a[href]').forEach((anchor) => {
    const href = anchor.getAttribute('href') || ''
    if (href.startsWith('/circulars/') || href.startsWith('/documents/')) {
      anchor.classList.add('document-pill', 'chat-citation-pill')
      anchor.dataset.documentLink = 'true'
    }
  })
  // A model comparing a dozen circulars emits a table wider than the bubble, and
  // an unwrapped one pushed the whole thread into a horizontal scroll.
  template.content.querySelectorAll('table').forEach((table) => {
    const wrapper = document.createElement('div')
    wrapper.className = 'markdown-table-scroll'
    table.replaceWith(wrapper)
    wrapper.append(table)
  })
  replaceBareCircularIds(template.content)

  const html = template.innerHTML
  if (markdownCache.size > 400) {
    markdownCache.clear()
  }
  markdownCache.set(key, html)
  return html
}

async function handleCitationClick(event: MouseEvent) {
  const target = event.target instanceof Element ? event.target.closest<HTMLAnchorElement>('a[data-document-link="true"]') : null
  if (!target) return
  event.preventDefault()
  const href = target.getAttribute('href') || '/'
  if (!href.startsWith('/documents/open')) {
    void router.push(href)
    return
  }

  const id = new URL(href, window.location.origin).searchParams.get('id')
  if (!id) return
  try {
    const resolved = await resolveDocument({ id })
    if (resolved.file_type?.toLowerCase() === 'pdf') {
      selectedAttachment.value = resolved
      attachmentDialogVisible.value = true
    } else {
      window.open(buildDocumentContentUrl(resolved.id), '_blank', 'noopener,noreferrer')
    }
  } catch (error) {
    toast.add({
      severity: 'error',
      summary: 'Attachment unavailable',
      detail: error instanceof Error ? error.message : 'Unable to open the cached attachment.',
      life: 6000,
    })
  }
}

function messageClass(role: string): string {
  return role === 'user' ? 'chat-message user-message' : 'chat-message assistant-message'
}

function isSelected(id: string): boolean {
  return selectedCirculars.value.some((item) => item.id === id)
}

function addContext(circular: CircularSummary) {
  if (activeSessionIsWorkspace.value) {
    return
  }
  if (isSelected(circular.id)) {
    return
  }

  selectedCirculars.value = [...selectedCirculars.value, circular]
}

function removeContext(id: string) {
  if (activeSessionIsWorkspace.value) {
    return
  }
  selectedCirculars.value = selectedCirculars.value.filter((item) => item.id !== id)
}

function clearContextSearch() {
  contextQuery.value = ''
  searchResults.value = []
}

function updatePinnedState() {
  const el = messagesEl.value
  if (!el) return
  // 96px of slack: a reader who is essentially at the bottom still wants to be
  // carried along by new tokens.
  pinnedToBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 96
}

async function scrollToBottom(force = false) {
  if (!force && !pinnedToBottom.value) {
    return
  }
  await nextTick()
  await new Promise<void>((resolve) => {
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => resolve())
    })
  })
  if (messagesEl.value) {
    messagesEl.value.scrollTop = messagesEl.value.scrollHeight
    pinnedToBottom.value = true
  }
}

function focusComposer() {
  void nextTick(() => {
    composerShellEl.value?.querySelector('textarea')?.focus()
  })
}

async function loadSessions() {
  sessionsLoading.value = true

  try {
    sessions.value = await getChatSessions()
  } catch (error) {
    toast.add({
      severity: 'error',
      summary: 'Sessions unavailable',
      detail: error instanceof Error ? error.message : 'Unable to load chat sessions.',
      life: 6000,
    })
  } finally {
    sessionsLoading.value = false
  }
}

// No `sending` guard here: this is the reconciliation step that runs at the end of
// a turn, while `sending` is still true. Guarding it left every freshly generated
// message stuck on its client-side id with `pending` never cleared, which is why
// finished answers read "Sending" and had no regenerate/delete actions.
async function loadSession(sessionId: string) {
  sessionLoading.value = true
  errorMessage.value = ''
  let loaded = false

  try {
    const data = await getChatSession(sessionId)
    currentSessionId.value = data.id
    messages.value = data.messages
    selectedCirculars.value = data.circulars || []
    clearContextSearch()
    renamingSessionId.value = null
    loaded = true
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'Unable to load chat session.'
  } finally {
    sessionLoading.value = false
  }

  if (loaded) {
    await scrollToBottom(true)
  }
}

function openSession(session: ChatSession) {
  if (sending.value) return
  railOpen.value = false
  if (session.id === currentSessionId.value) return
  void router.push(`/chat/${encodeURIComponent(session.id)}`)
}

function newSession() {
  if (sending.value) return
  currentSessionId.value = null
  messages.value = []
  selectedCirculars.value = []
  inputMessage.value = ''
  errorMessage.value = ''
  stoppedNotice.value = false
  turnSteps.value = []
  editingMessageId.value = null
  renamingSessionId.value = null
  railOpen.value = false
  if (route.params.sessionId) {
    void router.push('/chat')
  }
  focusComposer()
}

function startRenaming(session: ChatSession) {
  if (isWorkspaceSession(session)) return
  renamingSessionId.value = session.id
  renameDraft.value = session.title || 'New chat'
}

async function saveSessionTitle(sessionId: string) {
  const title = renameDraft.value.trim()
  if (!title) return

  try {
    await renameChatSession(sessionId, title)
    renamingSessionId.value = null
    await loadSessions()
  } catch (error) {
    toast.add({
      severity: 'error',
      summary: 'Rename failed',
      detail: error instanceof Error ? error.message : 'Unable to rename this session.',
      life: 6000,
    })
  }
}

function confirmDeleteSession(session: ChatSession) {
  const workspaceSession = isWorkspaceSession(session)
  const defaultWorkspace = workspaceSession && session.is_default_workspace
  confirm.require({
    message: defaultWorkspace
      ? 'Clear the Default workspace chat and remove its attached circulars?'
      : workspaceSession
        ? `Delete "${session.title || 'Workspace'}" and its chat history?`
        : 'Delete this chat session?',
    header: defaultWorkspace ? 'Clear Default session' : workspaceSession ? 'Delete workspace' : 'Delete session',
    icon: 'pi pi-trash',
    rejectProps: {
      label: 'Cancel',
      severity: 'secondary',
      outlined: true,
    },
    acceptProps: {
      label: 'Delete',
      severity: 'danger',
    },
    accept: () => {
      void removeSession(session)
    },
  })
}

async function removeSession(session: ChatSession) {
  const sessionId = session.id
  try {
    await deleteChatSession(sessionId)
    await loadSessions()
    if (currentSessionId.value === sessionId && session.is_default_workspace) {
      await loadSession(sessionId)
    } else if (currentSessionId.value === sessionId) {
      newSession()
    }
    toast.add({
      severity: 'success',
      summary: session.is_default_workspace ? 'Default session cleared' : 'Session deleted',
      life: 2500,
    })
  } catch (error) {
    toast.add({
      severity: 'error',
      summary: 'Delete failed',
      detail: error instanceof Error ? error.message : 'Unable to delete this session.',
      life: 6000,
    })
  }
}

async function copyMessage(message: LocalMessage) {
  try {
    await navigator.clipboard.writeText(messageToPlainText(message.content))
    toast.add({ severity: 'success', summary: 'Copied', life: 1800 })
  } catch {
    toast.add({ severity: 'error', summary: 'Copy failed', life: 4000 })
  }
}

/** Long enumerations (a 50-item "which circulars require X" answer) are the
    reason this exists — they are unusable as scrollback but fine as a file. */
function downloadMessage(message: LocalMessage) {
  const heading = activeSession.value?.title || 'SBPEye answer'
  const body = `# ${heading}\n\n${messageToPlainText(message.content)}\n`
  const blob = new Blob([body], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `sbpeye-answer-${new Date().toISOString().slice(0, 10)}.md`
  anchor.click()
  URL.revokeObjectURL(url)
}

function startEditing(message: LocalMessage) {
  editingMessageId.value = message.id
  editDraft.value = message.content
}

function confirmDeleteMessage(message: LocalMessage) {
  if (!currentSessionId.value) return
  confirm.require({
    message: 'Delete this message and all responses after it?',
    header: 'Delete conversation history',
    icon: 'pi pi-trash',
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    acceptProps: { label: 'Delete', severity: 'danger' },
    accept: () => void deleteMessage(message),
  })
}

async function deleteMessage(message: LocalMessage) {
  if (!currentSessionId.value) return
  try {
    await truncateChatSession(currentSessionId.value, message.id)
    await loadSession(currentSessionId.value)
    await loadSessions()
  } catch (error) {
    toast.add({
      severity: 'error',
      summary: 'Delete failed',
      detail: error instanceof Error ? error.message : 'Unable to delete this message.',
      life: 6000,
    })
  }
}

async function searchCirculars() {
  if (activeSessionIsWorkspace.value) {
    searchResults.value = []
    return
  }

  const query = contextQuery.value.trim()
  if (query.length < 2) {
    searchResults.value = []
    return
  }

  searchLoading.value = true

  try {
    const response = await getCircularSearch({ q: query, page: 1, per_page: 8 })
    searchResults.value = response.items
  } catch {
    searchResults.value = []
  } finally {
    searchLoading.value = false
  }
}

async function loadCircularContextFromQuery() {
  const raw = route.query.circular_ids
  const value = Array.isArray(raw) ? raw.join(',') : raw
  if (!value) {
    return
  }

  const ids = value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)

  for (const id of ids) {
    if (isSelected(id)) {
      continue
    }

    try {
      const circular = await getCircularDetail(id)
      addContext(circular)
    } catch (error) {
      toast.add({
        severity: 'warn',
        summary: 'Context skipped',
        detail: error instanceof Error ? error.message : `Unable to load circular ${id}.`,
        life: 5000,
      })
    }
  }

  await router.replace({ path: '/chat' })
}

async function loadWorkspaceSessionFromQuery(): Promise<boolean> {
  const raw = route.query.workspace
  const workspaceId = Array.isArray(raw) ? raw[0] : raw
  if (!workspaceId) {
    return false
  }

  const workspaceSession = sessions.value.find((session) => session.workspace_id === workspaceId)
  if (!workspaceSession) {
    toast.add({
      severity: 'warn',
      summary: 'Workspace unavailable',
      detail: 'Unable to load that workspace chat.',
      life: 5000,
    })
    await router.replace({ path: '/chat' })
    return true
  }

  await router.replace(`/chat/${encodeURIComponent(workspaceSession.id)}`)
  await loadSession(workspaceSession.id)
  return true
}

/** After an abort the server writes its partial from generator teardown, which can
    land a beat after the fetch rejects. Poll briefly so the reconciled thread
    already contains it instead of dropping it until the next reload. */
async function awaitPersistedTurn(sessionId: string, expectedAssistants: number) {
  const deadline = Date.now() + 1500
  while (Date.now() < deadline) {
    try {
      const data = await getChatSession(sessionId)
      if (data.messages.filter((message) => message.role === 'assistant').length >= expectedAssistants) {
        return
      }
    } catch {
      // Keep polling; the reconciling load reports any real failure.
    }
    await new Promise((resolve) => window.setTimeout(resolve, 200))
  }
}

function startElapsedTimer() {
  elapsedSeconds.value = 0
  window.clearInterval(elapsedTimer)
  elapsedTimer = window.setInterval(() => {
    elapsedSeconds.value += 1
  }, 1000)
}

function stopElapsedTimer() {
  window.clearInterval(elapsedTimer)
  elapsedTimer = undefined
}

async function generateMessage(
  textValue: string,
  replaceMessage?: LocalMessage,
) {
  const text = textValue.trim()
  if (!text || sending.value) {
    return
  }

  errorMessage.value = ''
  stoppedNotice.value = false
  activityLabel.value = 'Thinking'
  turnSteps.value = []
  turnStepsSessionId.value = currentSessionId.value
  sending.value = true
  startElapsedTimer()
  editingMessageId.value = null
  const circularIds = replaceMessage?.circular_ids?.length
    && !activeSessionIsWorkspace.value
    ? replaceMessage.circular_ids
    : selectedCirculars.value.map((item) => item.id)

  if (replaceMessage) {
    const targetIndex = messages.value.findIndex((message) => message.id === replaceMessage.id)
    messages.value = [
      ...messages.value.slice(0, targetIndex),
      { ...replaceMessage, content: text, pending: false },
    ]
  } else {
    messages.value = [
      ...messages.value,
      {
        id: `local-${Date.now()}`,
        role: 'user',
        content: text,
        circular_ids: circularIds,
        pending: false,
      },
    ]
  }
  await scrollToBottom(true)

  const assistantId = `assistant-${Date.now()}`
  try {
    messages.value = [
      ...messages.value,
      {
        id: assistantId,
        role: 'assistant',
        content: '',
        pending: true,
      },
    ]
    await scrollToBottom(true)

    streamController = new AbortController()
    await streamChatMessage(
      {
        message: text,
        session_id: currentSessionId.value,
        circular_ids: circularIds,
        replace_message_id: replaceMessage?.id,
      },
      {
        onSession: (sessionId) => {
          currentSessionId.value = sessionId
          turnStepsSessionId.value = sessionId
          if (route.params.sessionId !== sessionId) {
            void router.replace(`/chat/${encodeURIComponent(sessionId)}`)
          }
        },
        onStatus: (status) => {
          if (status.phase === 'tools' && status.tools?.length) {
            // The model narrates before each tool call. That narration is progress,
            // not answer text, so it becomes a step rather than being spliced into
            // the reply (which is how answers used to acquire "…and reportingLet me
            // fetch the specific circular" run-ons).
            turnSteps.value = [...turnSteps.value, { tools: status.tools, note: status.note || '' }]
            activityLabel.value = status.tools.join(' · ')
            messages.value = messages.value.map((message) =>
              message.id === assistantId ? { ...message, content: '' } : message,
            )
          } else if (status.phase === 'thinking') {
            activityLabel.value = turnSteps.value.length ? 'Reading results' : 'Thinking'
          }
          void scrollToBottom()
        },
        onToken: (content) => {
          activityLabel.value = 'Writing answer'
          messages.value = messages.value.map((message) =>
            message.id === assistantId ? { ...message, content: message.content + content } : message,
          )
          void scrollToBottom()
        },
        onError: (message) => {
          errorMessage.value = message
        },
        onDone: (sessionId) => {
          if (sessionId) {
            currentSessionId.value = sessionId
          }
        },
      },
      streamController.signal,
    )
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      stoppedNotice.value = true
    } else {
      errorMessage.value = error instanceof Error ? error.message : 'Unable to send message.'
    }
  } finally {
    streamController = null
    sending.value = false
    activityLabel.value = ''
    stopElapsedTimer()

    // loadSession clears the error banner as part of a clean load, so a failure
    // reported by the stream has to be carried across the reconciliation.
    const turnError = errorMessage.value

    if (currentSessionId.value) {
      // Only Stop races the server: on the success and error paths the write has
      // already happened by the time the stream ends.
      if (stoppedNotice.value) {
        await awaitPersistedTurn(
          currentSessionId.value,
          messages.value.filter((message) => message.role === 'assistant').length,
        )
      }
      await loadSession(currentSessionId.value)
    } else {
      messages.value = messages.value.filter((message) => message.id !== assistantId)
    }

    // Nothing is in flight any more, whatever happened. A successful reconcile
    // returns fresh server rows with no pending flag; a failed one leaves the
    // local placeholder behind, and it must not keep claiming to be working.
    messages.value = messages.value
      .filter((message) => !(message.pending && message.role === 'assistant' && !message.content.trim()))
      .map((message) => (message.pending ? { ...message, pending: false } : message))

    if (turnError) {
      errorMessage.value = turnError
    }
    await loadSessions()
    await scrollToBottom(true)
  }
}

function sendMessage() {
  if (sending.value) return
  const text = inputMessage.value
  if (!text.trim()) return
  inputMessage.value = ''
  void generateMessage(text)
}

const examplePrompts = computed(() => (hasContext.value
  ? [
    'Summarise the key obligations in the attached documents.',
    'What deadlines or reporting dates do these impose?',
    'Have any of these been amended or superseded?',
  ]
  : [
    'What are the minimum capital adequacy requirements for banks?',
    'Which circulars has BPRD issued on Basel III implementation?',
    'What is Enhanced Due Diligence and when is it required?',
  ]))

function useExamplePrompt(text: string) {
  if (sending.value) return
  inputMessage.value = ''
  void generateMessage(text)
}

function saveEditedMessage(message: LocalMessage) {
  const text = editDraft.value.trim()
  if (!text || text === message.content) {
    editingMessageId.value = null
    return
  }
  confirm.require({
    message: 'Editing this message will replace all responses after it. Continue?',
    header: 'Edit conversation history',
    icon: 'pi pi-pencil',
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    acceptProps: { label: 'Edit and regenerate' },
    accept: () => void generateMessage(text, message),
  })
}

function regenerateMessage(assistantIndex: number) {
  const userMessage = [...messages.value.slice(0, assistantIndex)]
    .reverse()
    .find((message) => message.role === 'user')
  if (!userMessage) return
  if (assistantIndex < messages.value.length - 1) {
    confirm.require({
      message: 'Regenerating this response will replace all later messages. Continue?',
      header: 'Regenerate response',
      icon: 'pi pi-refresh',
      rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
      acceptProps: { label: 'Regenerate' },
      accept: () => void generateMessage(userMessage.content, userMessage),
    })
    return
  }
  void generateMessage(userMessage.content, userMessage)
}

function retryMessage(message: LocalMessage) {
  void generateMessage(message.content, message)
}

function retryLastTurn() {
  const message = lastUserMessage.value
  if (!message) return
  errorMessage.value = ''
  void generateMessage(message.content, message)
}

function stopGeneration() {
  streamController?.abort()
}

function isUnansweredUser(index: number): boolean {
  return messages.value[index].role === 'user'
    && (!messages.value[index + 1] || messages.value[index + 1].role !== 'assistant')
}

/** Steps belong to the turn that just ran, so they render under the last answer
    of the session they were produced in and nowhere else. */
function showsTurnSteps(index: number): boolean {
  return turnSteps.value.length > 0
    && turnStepsSessionId.value === currentSessionId.value
    && messages.value[index].role === 'assistant'
    && index === messages.value.length - 1
}

watch(contextQuery, () => {
  if (searchTimer) {
    window.clearTimeout(searchTimer)
  }
  searchTimer = window.setTimeout(() => {
    void searchCirculars()
  }, 250)
})

watch(selectedCirculars, (circulars) => {
  circulars.forEach(cacheCircularCitation)
}, { immediate: true, deep: true })

watch(messages, preloadCircularCitations, { deep: true })

// A resolved label changes what a citation pill should say, so memoised renders
// have to be invalidated when the cache grows.
watch(circularCitationCache, () => {
  citationVersion.value += 1
}, { deep: true })

watch(() => route.params.sessionId, (raw) => {
  const sessionId = Array.isArray(raw) ? raw[0] : raw
  if (!sessionId) return
  const decoded = decodeURIComponent(sessionId)
  if (decoded === currentSessionId.value || sending.value) return
  void loadSession(decoded)
})

onMounted(async () => {
  await loadSessions()
  const raw = route.params.sessionId
  const sessionId = Array.isArray(raw) ? raw[0] : raw
  if (sessionId) {
    await loadSession(decodeURIComponent(sessionId))
    focusComposer()
    return
  }
  if (await loadWorkspaceSessionFromQuery()) return
  await loadCircularContextFromQuery()
  await scrollToBottom(true)
  focusComposer()
})

onBeforeUnmount(() => {
  stopElapsedTimer()
  window.clearTimeout(searchTimer)
  streamController?.abort()
})
</script>
<template>
  <div class="chat-view sbp-pane-view" :class="{ 'rail-open': railOpen }">
    <aside class="chat-rail sbp-rail">
      <header class="sbp-rail-head">
        <span class="sbp-rail-title">Conversations</span>
        <span class="sbp-rail-count">{{ visibleSessionCount || '—' }}</span>
      </header>

      <Button
        label="New chat"
        icon="pi pi-plus"
        size="small"
        fluid
        outlined
        :disabled="sending"
        @click="newSession"
      />

      <div v-if="sessions.length > 6" class="sbp-search-field rail-search-field">
        <i class="pi pi-search" />
        <input
          v-model="sessionFilter"
          type="search"
          placeholder="Filter conversations"
          aria-label="Filter conversations"
        >
        <button
          v-if="sessionFilter"
          type="button"
          class="sbp-search-clear"
          aria-label="Clear conversation filter"
          @click="sessionFilter = ''"
        ><i class="pi pi-times" /></button>
      </div>

      <p v-if="sessionsLoading" class="sbp-note">Loading sessions…</p>
      <p v-else-if="!sessions.length" class="sbp-note">No saved chat sessions.</p>
      <p v-else-if="!visibleSessionCount" class="sbp-note">No conversations match “{{ sessionFilter }}”.</p>

      <div v-else class="session-list">
        <template v-for="group in sessionGroups" :key="group.label">
          <p class="session-group-label">{{ group.label }}</p>
          <div
            v-for="session in group.sessions"
            :key="session.id"
            class="session-item"
            :class="{ 'is-selected': session.id === currentSessionId, workspace: isWorkspaceSession(session) }"
          >
            <form
              v-if="renamingSessionId === session.id"
              class="session-rename"
              @submit.prevent="saveSessionTitle(session.id)"
            >
              <InputText
                v-model="renameDraft"
                size="small"
                autofocus
                maxlength="120"
                aria-label="Session title"
                @keydown.escape="renamingSessionId = null"
              />
              <Button icon="pi pi-check" type="submit" size="small" text rounded aria-label="Save session title" />
              <Button icon="pi pi-times" type="button" size="small" text rounded severity="secondary" aria-label="Cancel rename" @click="renamingSessionId = null" />
            </form>
            <template v-else>
              <button
                type="button"
                class="sbp-row session-open"
                :disabled="sending"
                :aria-current="session.id === currentSessionId ? 'true' : undefined"
                @click="openSession(session)"
              >
                <span class="sbp-row-title session-name">
                  <i class="pi" :class="sessionIconClass(session)" />
                  <span>{{ session.title || 'New chat' }}</span>
                </span>
                <span class="sbp-row-sub">
                  <template v-if="isWorkspaceSession(session)">
                    {{ (session.pinned_count || 0).toLocaleString() }} pinned
                  </template>
                  <template v-else>
                    {{ sessionDate(session) }}
                  </template>
                </span>
              </button>
              <div class="session-item-actions">
                <Button
                  v-if="!isWorkspaceSession(session)"
                  icon="pi pi-pencil"
                  text
                  rounded
                  size="small"
                  severity="secondary"
                  aria-label="Rename session"
                  @click="startRenaming(session)"
                />
                <Button
                  icon="pi pi-trash"
                  text
                  rounded
                  size="small"
                  severity="danger"
                  aria-label="Delete session"
                  @click="confirmDeleteSession(session)"
                />
              </div>
            </template>
          </div>
        </template>
      </div>
    </aside>

    <section class="chat-pane">
      <!-- Context strip: the same role the search-controls bar plays on Circulars. -->
      <header class="chat-context-bar">
        <div class="context-header">
          <Button
            class="rail-toggle"
            :icon="railOpen ? 'pi pi-times' : 'pi pi-bars'"
            text
            rounded
            size="small"
            severity="secondary"
            :aria-label="railOpen ? 'Hide conversations' : 'Show conversations'"
            :aria-expanded="railOpen"
            @click="railOpen = !railOpen"
          />
          <span class="sbp-badge" :class="{ 'is-accent': hasContext }" :title="contextModeHint">
            {{ contextModeLabel }}
          </span>

          <div v-if="activeSessionIsWorkspace" class="workspace-context-note">
            <i class="pi pi-thumbtack" />
            <span>Pins follow the workspace</span>
          </div>

          <div v-else class="sbp-search-field context-search-field">
            <i class="pi pi-search" />
            <input
              v-model="contextQuery"
              type="search"
              placeholder="Attach circulars for a narrower answer"
              aria-label="Search circulars to attach as context"
            >
            <button
              v-if="contextQuery"
              type="button"
              class="sbp-search-clear"
              aria-label="Clear context search"
              @click="clearContextSearch"
            ><i class="pi pi-times" /></button>
          </div>
        </div>

        <div v-if="selectedCirculars.length" class="context-chip-list">
          <RouterLink
            v-for="circular in selectedCirculars"
            :key="circular.id"
            class="context-chip"
            :to="`/circulars/${encodeURIComponent(circular.id)}`"
            :title="circular.title"
          >
            <span>{{ circular.reference || circular.title }}</span>
            <button
              v-if="!activeSessionIsWorkspace"
              type="button"
              :aria-label="`Remove ${circular.title}`"
              @click.prevent.stop="removeContext(circular.id)"
            >
              <i class="pi pi-times" />
            </button>
          </RouterLink>
          <button
            v-if="!activeSessionIsWorkspace && selectedCirculars.length > 1"
            type="button"
            class="context-clear-all"
            @click="selectedCirculars = []"
          >Clear all</button>
        </div>

        <div v-if="searchLoading" class="context-search-state">
          <i class="pi pi-spin pi-spinner" />
          <span>Searching circulars</span>
        </div>

        <div v-else-if="!activeSessionIsWorkspace && searchResults.length" class="context-results">
          <button
            v-for="result in searchResults"
            :key="result.id"
            type="button"
            class="circular-result-item"
            :class="{ 'is-attached': isSelected(result.id) }"
            :disabled="isSelected(result.id)"
            @click="addContext(result)"
          >
            <span class="result-select result-action-icon">
              <i class="pi" :class="isSelected(result.id) ? 'pi-check' : 'pi-plus'" />
            </span>
            <CircularResultContent :circular="result" />
          </button>
        </div>
      </header>

      <div
        ref="messagesEl"
        class="chat-messages"
        role="log"
        aria-live="polite"
        aria-label="Conversation"
        @scroll.passive="updatePinnedState"
      >
        <div v-if="sessionLoading" class="preview-loading compact-loading">
          <ProgressSpinner aria-label="Loading conversation" />
          <span>Loading conversation</span>
        </div>

        <div v-else-if="!messages.length" class="chat-empty-state">
          <span class="chat-empty-icon"><i class="pi pi-comments" /></span>
          <h2>Ask SBPEye about SBP circulars</h2>
          <p>
            {{ hasContext
              ? 'Your attached circulars are searched first. Ask a question, or try one of these:'
              : 'Answers search the whole circular index. Attach documents above to narrow the scope, or try one of these:' }}
          </p>
          <div class="chat-empty-prompts">
            <button
              v-for="prompt in examplePrompts"
              :key="prompt"
              type="button"
              class="chat-empty-prompt"
              :disabled="sending"
              @click="useExamplePrompt(prompt)"
            >
              <i class="pi pi-arrow-right" />
              <span>{{ prompt }}</span>
            </button>
          </div>
        </div>

        <template v-else>
          <article
            v-for="(message, index) in messages"
            :key="message.id"
            :class="messageClass(message.role)"
          >
            <div class="chat-message-meta">
              <strong>{{ message.role === 'user' ? 'You' : 'Assistant' }}</strong>
              <div class="message-actions">
                <Button
                  v-if="!message.pending || message.content"
                  icon="pi pi-copy"
                  text
                  rounded
                  size="small"
                  severity="secondary"
                  aria-label="Copy message"
                  title="Copy as plain text"
                  @click="copyMessage(message)"
                />
                <Button
                  v-if="message.role === 'assistant' && !message.pending && message.content"
                  icon="pi pi-download"
                  text
                  rounded
                  size="small"
                  severity="secondary"
                  aria-label="Download answer as markdown"
                  title="Download as Markdown"
                  @click="downloadMessage(message)"
                />
                <Button
                  v-if="message.role === 'user' && !message.pending"
                  icon="pi pi-pencil"
                  text
                  rounded
                  size="small"
                  severity="secondary"
                  aria-label="Edit message"
                  title="Edit and regenerate"
                  :disabled="sending"
                  @click="startEditing(message)"
                />
                <Button
                  v-if="message.role === 'assistant' && !message.pending"
                  icon="pi pi-refresh"
                  text
                  rounded
                  size="small"
                  severity="secondary"
                  aria-label="Regenerate response"
                  title="Regenerate"
                  :disabled="sending"
                  @click="regenerateMessage(index)"
                />
                <Button
                  v-if="!message.pending"
                  icon="pi pi-trash"
                  text
                  rounded
                  size="small"
                  severity="danger"
                  aria-label="Delete message and following history"
                  title="Delete from here"
                  :disabled="sending"
                  @click="confirmDeleteMessage(message)"
                />
              </div>
            </div>

            <!-- Tool rounds, shown above the answer they produced. -->
            <details
              v-if="(message.pending || showsTurnSteps(index)) && turnSteps.length"
              class="assistant-steps"
            >
              <summary>
                <i class="pi pi-wrench" />
                <span>{{ turnSteps.length }} research {{ turnSteps.length === 1 ? 'step' : 'steps' }}</span>
                <i class="pi pi-chevron-right steps-chevron" />
              </summary>
              <ol>
                <li v-for="(step, stepIndex) in turnSteps" :key="stepIndex">
                  <span class="step-tools">{{ step.tools.join(' · ') }}</span>
                  <p v-if="step.note">{{ step.note }}</p>
                </li>
              </ol>
            </details>

            <div v-if="editingMessageId === message.id" class="message-editor">
              <Textarea v-model="editDraft" rows="3" auto-resize autofocus />
              <div class="message-editor-actions">
                <Button label="Cancel" size="small" text severity="secondary" @click="editingMessageId = null" />
                <Button label="Save and regenerate" size="small" @click="saveEditedMessage(message)" />
              </div>
            </div>
            <div
              v-else-if="message.role === 'assistant'"
              class="markdown-body"
              v-html="renderMarkdown(message.content)"
              @click="handleCitationClick"
            />
            <p v-else>{{ message.content }}</p>

            <div v-if="message.pending" class="assistant-activity">
              <i class="pi pi-spin pi-spinner" />
              <span>{{ activityLabel || 'Working' }}</span>
              <span class="activity-elapsed">{{ elapsedLabel }}</span>
            </div>

            <Button
              v-if="isUnansweredUser(index) && !sending"
              class="retry-message"
              icon="pi pi-refresh"
              label="Retry"
              size="small"
              text
              @click="retryMessage(message)"
            />
          </article>

          <!-- Failures belong next to the turn that failed, not pinned above the
               whole thread where they outlive the problem. -->
          <div v-if="errorMessage" class="turn-error" role="alert">
            <i class="pi pi-exclamation-triangle" />
            <div class="turn-error-body">
              <p>{{ errorMessage }}</p>
              <Button
                v-if="lastUserMessage && !sending"
                label="Retry"
                icon="pi pi-refresh"
                size="small"
                text
                @click="retryLastTurn"
              />
            </div>
            <Button
              icon="pi pi-times"
              text
              rounded
              size="small"
              severity="secondary"
              aria-label="Dismiss error"
              @click="errorMessage = ''"
            />
          </div>

          <p v-else-if="stoppedNotice && !sending" class="turn-stopped">
            Generation stopped. Anything already written was kept.
          </p>
        </template>
      </div>

      <Transition name="jump-fade">
        <button
          v-if="!pinnedToBottom && messages.length"
          type="button"
          class="jump-latest"
          @click="scrollToBottom(true)"
        >
          <i class="pi pi-arrow-down" />
          <span>Jump to latest</span>
        </button>
      </Transition>

      <form class="composer" @submit.prevent="sendMessage">
        <div ref="composerShellEl" class="composer-shell">
          <Textarea
            v-model="inputMessage"
            rows="2"
            auto-resize
            :placeholder="hasContext ? 'Ask about the attached circulars' : 'Ask about SBP circulars and regulations'"
            aria-label="Message"
            @keydown.enter.exact.prevent="sendMessage"
          />
          <div class="composer-actions">
            <span class="composer-hint">
              <kbd>Enter</kbd> to send · <kbd>Shift</kbd>+<kbd>Enter</kbd> for a new line
            </span>
            <Button
              v-if="sending"
              icon="pi pi-stop"
              label="Stop"
              type="button"
              size="small"
              severity="danger"
              outlined
              @click="stopGeneration"
            />
            <Button
              v-else
              icon="pi pi-send"
              label="Send"
              type="submit"
              size="small"
              :disabled="!inputMessage.trim()"
            />
          </div>
        </div>
      </form>
    </section>
  </div>
  <PdfPreviewDialog
    v-if="selectedAttachment"
    v-model:visible="attachmentDialogVisible"
    :title="selectedAttachment.filename"
    :document-id="selectedAttachment.id"
  />
</template>
