<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import Button from 'primevue/button'
import Card from 'primevue/card'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import ProgressSpinner from 'primevue/progressspinner'
import Tag from 'primevue/tag'
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
const sessionsLoading = ref(false)
const sessionLoading = ref(false)
const searchLoading = ref(false)
const sending = ref(false)
const editingMessageId = ref<string | null>(null)
const editDraft = ref('')
const renamingSessionId = ref<string | null>(null)
const renameDraft = ref('')
const errorMessage = ref('')
const messagesEl = ref<HTMLElement | null>(null)
const attachmentDialogVisible = ref(false)
const selectedAttachment = ref<ResolvedDocument | null>(null)
let searchTimer: number | undefined
let streamController: AbortController | null = null

marked.use({
  breaks: true,
  gfm: true,
})

const hasContext = computed(() => selectedCirculars.value.length > 0)
const contextModeLabel = computed(() => (hasContext.value ? 'Grounded' : 'Ungrounded'))

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

function sessionDate(session: ChatSession): string {
  return formatDate(session.updated_at || session.created_at)
}

function renderMarkdown(content: string): string {
  const withCitations = content.replace(
    /\[\[(circular|attachment):([a-zA-Z0-9-]+)\|([^\]\n]+)\]\]/g,
    (_match, kind: string, id: string, label: string) => {
      const href = kind === 'circular'
        ? `/circulars/${encodeURIComponent(id)}`
        : `/documents/open?id=${encodeURIComponent(id)}`
      const icon = kind === 'circular' ? 'pi-file' : 'pi-paperclip'
      const safeLabel = label.replace(/[&<>"']/g, (character) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
      }[character] || character))
      return `<a href="${href}" class="document-pill chat-citation-pill" data-document-link="true"><i class="pi ${icon}"></i><span>${safeLabel}</span></a>`
    },
  )
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
  return template.innerHTML
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
    const document = await resolveDocument({ id })
    if (document.file_type?.toLowerCase() === 'pdf') {
      selectedAttachment.value = document
      attachmentDialogVisible.value = true
    } else {
      window.open(buildDocumentContentUrl(document.id), '_blank', 'noopener,noreferrer')
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
  if (isSelected(circular.id)) {
    return
  }

  selectedCirculars.value = [...selectedCirculars.value, circular]
}

function removeContext(id: string) {
  selectedCirculars.value = selectedCirculars.value.filter((item) => item.id !== id)
}

async function scrollToBottom() {
  await nextTick()
  if (messagesEl.value) {
    messagesEl.value.scrollTop = messagesEl.value.scrollHeight
  }
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

async function loadSession(sessionId: string) {
  if (sending.value) return
  sessionLoading.value = true
  errorMessage.value = ''

  try {
    const data = await getChatSession(sessionId)
    currentSessionId.value = data.id
    messages.value = data.messages
    selectedCirculars.value = data.circulars || []
    await scrollToBottom()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'Unable to load chat session.'
  } finally {
    sessionLoading.value = false
  }
}

function newSession() {
  if (sending.value) return
  currentSessionId.value = null
  messages.value = []
  selectedCirculars.value = []
  inputMessage.value = ''
  errorMessage.value = ''
  editingMessageId.value = null
  renamingSessionId.value = null
}

function startRenaming(session: ChatSession) {
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

function confirmDeleteSession(sessionId: string) {
  confirm.require({
    message: 'Delete this chat session?',
    header: 'Delete session',
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
      void removeSession(sessionId)
    },
  })
}

async function removeSession(sessionId: string) {
  try {
    await deleteChatSession(sessionId)
    if (currentSessionId.value === sessionId) {
      newSession()
    }
    await loadSessions()
    toast.add({
      severity: 'success',
      summary: 'Session deleted',
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

async function copyMessage(content: string) {
  try {
    await navigator.clipboard.writeText(content)
    toast.add({ severity: 'success', summary: 'Copied', life: 1800 })
  } catch {
    toast.add({ severity: 'error', summary: 'Copy failed', life: 4000 })
  }
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

async function generateMessage(
  textValue: string,
  replaceMessage?: LocalMessage,
) {
  const text = textValue.trim()
  if (!text || sending.value) {
    return
  }

  errorMessage.value = ''
  sending.value = true
  editingMessageId.value = null
  const circularIds = replaceMessage?.circular_ids?.length
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
        pending: true,
      },
    ]
  }
  await scrollToBottom()

  try {
    const assistantId = `assistant-${Date.now()}`
    messages.value = [
      ...messages.value.map((message) => (message.pending ? { ...message, pending: false } : message)),
      {
        id: assistantId,
        role: 'assistant',
        content: '',
        pending: true,
      },
    ]
    await scrollToBottom()

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
        },
        onToken: (content) => {
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

    if (currentSessionId.value) {
      await loadSession(currentSessionId.value)
    }
    await loadSessions()
    await scrollToBottom()
  } catch (error) {
    if (currentSessionId.value) {
      await loadSession(currentSessionId.value)
    } else {
      messages.value = messages.value.filter((message) => !message.pending)
    }
    if (!(error instanceof DOMException && error.name === 'AbortError')) {
      errorMessage.value = error instanceof Error ? error.message : 'Unable to send message.'
    }
  } finally {
    streamController = null
    sending.value = false
  }
}

function sendMessage() {
  const text = inputMessage.value
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

function stopGeneration() {
  streamController?.abort()
}

function isUnansweredUser(index: number): boolean {
  return messages.value[index].role === 'user'
    && (!messages.value[index + 1] || messages.value[index + 1].role !== 'assistant')
}

watch(contextQuery, () => {
  if (searchTimer) {
    window.clearTimeout(searchTimer)
  }
  searchTimer = window.setTimeout(() => {
    void searchCirculars()
  }, 250)
})

onMounted(async () => {
  await loadSessions()
  await loadCircularContextFromQuery()
})
</script>

<template>
  <section class="view-stack chat-view">
    <div class="chat-layout">
      <Card class="session-panel glass-panel">
        <template #content>
          <div class="session-panel-actions">
            <Button label="New" icon="pi pi-plus" size="small" fluid :disabled="sending" @click="newSession" />
          </div>

          <div v-if="sessionsLoading" class="preview-loading compact-loading">
            <ProgressSpinner aria-label="Loading sessions" />
            <span>Loading sessions</span>
          </div>

          <div v-else-if="sessions.length" class="session-list">
            <div
              v-for="session in sessions"
              :key="session.id"
              class="session-item animated-card"
              :class="{ active: session.id === currentSessionId }"
            >
              <form
                v-if="renamingSessionId === session.id"
                class="session-rename"
                @submit.prevent="saveSessionTitle(session.id)"
              >
                <InputText
                  v-model="renameDraft"
                  autofocus
                  maxlength="120"
                  aria-label="Session title"
                  @keydown.escape="renamingSessionId = null"
                />
                <Button icon="pi pi-check" type="submit" text rounded aria-label="Save session title" />
                <Button icon="pi pi-times" type="button" text rounded severity="secondary" aria-label="Cancel rename" @click="renamingSessionId = null" />
              </form>
              <template v-else>
                <button type="button" :disabled="sending" @click="loadSession(session.id)">
                  <strong>{{ session.title || 'New chat' }}</strong>
                  <span>{{ sessionDate(session) }}</span>
                </button>
                <div class="session-item-actions">
                  <Button
                    icon="pi pi-pencil"
                    text
                    rounded
                    severity="secondary"
                    aria-label="Rename session"
                    @click="startRenaming(session)"
                  />
                  <Button
                    icon="pi pi-trash"
                    text
                    rounded
                    severity="danger"
                    aria-label="Delete session"
                    @click="confirmDeleteSession(session.id)"
                  />
                </div>
              </template>
            </div>
          </div>

          <Message v-else severity="secondary" :closable="false">
            No saved chat sessions.
          </Message>
        </template>
      </Card>

      <div class="chat-main">
        <Card class="context-card glass-panel">
          <template #content>
            <div class="context-header">
              <Tag :value="contextModeLabel" :severity="hasContext ? 'success' : 'secondary'" />
              <span>{{ selectedCirculars.length }} circulars selected</span>
            </div>

            <div v-if="selectedCirculars.length" class="context-chip-list">
              <Tag
                v-for="circular in selectedCirculars"
                :key="circular.id"
                severity="info"
                class="context-chip"
              >
                <span>{{ circular.reference || circular.title }}</span>
                <button type="button" :aria-label="`Remove ${circular.title}`" @click="removeContext(circular.id)">
                  <i class="pi pi-times" />
                </button>
              </Tag>
            </div>

            <div class="context-search">
              <span class="p-input-icon-left">
                <i class="pi pi-search" />
                <InputText v-model="contextQuery" placeholder="Search circulars to add context" />
              </span>

              <div v-if="searchLoading" class="context-search-state">
                <i class="pi pi-spin pi-spinner" />
                <span>Searching circulars</span>
              </div>

              <div v-else-if="searchResults.length" class="context-results">
                <button
                  v-for="result in searchResults"
                  :key="result.id"
                  type="button"
                  class="circular-result-item animated-card"
                  :disabled="isSelected(result.id)"
                  @click="addContext(result)"
                >
                  <span class="result-select result-action-icon">
                    <i class="pi" :class="isSelected(result.id) ? 'pi-check' : 'pi-plus'" />
                  </span>
                  <CircularResultContent :circular="result" />
                </button>
              </div>
            </div>
          </template>
        </Card>

        <Card class="conversation-card glass-panel">
          <template #content>
            <Message v-if="errorMessage" severity="error" :closable="false">
              {{ errorMessage }}
            </Message>

            <div ref="messagesEl" class="chat-messages">
              <div v-if="sessionLoading" class="preview-loading compact-loading">
                <ProgressSpinner aria-label="Loading conversation" />
                <span>Loading conversation</span>
              </div>

              <Message v-else-if="!messages.length" severity="secondary" :closable="false">
                Start a new question or select a saved session.
              </Message>

              <article
                v-for="(message, index) in messages"
                v-else
                :key="message.id"
                :class="messageClass(message.role)"
              >
                <div class="chat-message-meta">
                  <strong>{{ message.role === 'user' ? 'You' : 'Assistant' }}</strong>
                  <div class="message-actions">
                    <span v-if="message.pending">Sending</span>
                    <Button
                      icon="pi pi-copy"
                      text
                      rounded
                      size="small"
                      severity="secondary"
                      aria-label="Copy message"
                      title="Copy"
                      @click="copyMessage(message.content)"
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
                <Button
                  v-if="isUnansweredUser(index) && !message.pending"
                  class="retry-message"
                  icon="pi pi-refresh"
                  label="Retry"
                  size="small"
                  text
                  :disabled="sending"
                  @click="retryMessage(message)"
                />
              </article>

              <div v-if="sending" class="assistant-typing">
                <i class="pi pi-spin pi-spinner" />
                <span>Generating</span>
              </div>
            </div>

            <form class="composer" @submit.prevent="sendMessage">
              <Textarea
                v-model="inputMessage"
                rows="3"
                auto-resize
                placeholder="Ask about selected circulars"
                :disabled="sending"
                @keydown.enter.exact.prevent="sendMessage"
              />
              <div class="composer-actions">
                <Button
                  v-if="sending"
                  icon="pi pi-stop"
                  label="Stop"
                  type="button"
                  severity="danger"
                  outlined
                  @click="stopGeneration"
                />
                <Button
                  v-else
                  icon="pi pi-send"
                  label="Send"
                  type="submit"
                  :disabled="!inputMessage.trim()"
                />
              </div>
            </form>
          </template>
        </Card>
      </div>
    </div>
  </section>
  <PdfPreviewDialog
    v-if="selectedAttachment"
    v-model:visible="attachmentDialogVisible"
    :title="selectedAttachment.filename"
    :document-id="selectedAttachment.id"
  />
</template>
