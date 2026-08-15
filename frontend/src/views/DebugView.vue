<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Select from 'primevue/select'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import JsonInspector from '@/components/JsonInspector.vue'
import {
  deleteAllLlmTraces,
  deleteLlmTrace,
  getLlmTrace,
  getLlmTraces,
  type LlmTraceDetail,
  type LlmTraceEvent,
  type LlmTraceListResponse,
  type LlmTraceSummary,
} from '@/lib/api'
import { useLlmDebugState } from '@/lib/useLlmDebugState'

const confirm = useConfirm()
const toast = useToast()
const { state: gate, refreshLlmDebugState } = useLlmDebugState()

const loading = ref(false)
const error = ref('')
const paused = ref(false)
const selectedId = ref('')
const traces = ref<LlmTraceSummary[]>([])
const detail = ref<LlmTraceDetail | null>(null)
const facets = ref<LlmTraceListResponse['facets']>({ statuses: [], operations: [], origins: [], providers: [], models: [] })
const total = ref(0)
const page = ref(1)
const totalPages = ref(1)
const filters = reactive({ status: '', operation: '', origin: '', provider: '', model: '', correlation: '' })
let timer: ReturnType<typeof setInterval> | null = null
let controller: AbortController | null = null
let polling = false

const selected = computed(() => traces.value.find((item) => item.id === selectedId.value) || detail.value?.trace || null)
const selectOptions = (values: string[]) => values.map((value) => ({ label: friendly(value), value }))
const statusOptions = computed(() => selectOptions(facets.value.statuses))
const operationOptions = computed(() => selectOptions(facets.value.operations))
const originOptions = computed(() => selectOptions(facets.value.origins))
const providerOptions = computed(() => selectOptions(facets.value.providers))
const modelOptions = computed(() => selectOptions(facets.value.models))

function friendly(value: string) {
  return value.split(/[._]/).map((part) => part ? part[0].toUpperCase() + part.slice(1) : '').join(' ')
}

function formatDate(value?: string | null) {
  return value ? new Date(value).toLocaleString() : '—'
}

function formatDuration(trace: LlmTraceSummary) {
  const duration = trace.duration_ms ?? (trace.status === 'running' ? Date.now() - new Date(trace.started_at).getTime() : null)
  if (duration === null) return '—'
  return duration < 1000 ? `${duration} ms` : `${(duration / 1000).toFixed(1)} s`
}

function eventTone(kind: string) {
  if (kind.includes('error') || kind.includes('failed')) return 'is-error'
  if (kind === 'provider_request' || kind === 'tool_request') return 'is-request'
  if (kind === 'provider_response' || kind === 'tool_result') return 'is-response'
  if (kind === 'retry' || kind === 'structured_mode_changed') return 'is-warn'
  return 'is-context'
}

async function refresh(initial = false) {
  if (polling || (!initial && paused.value) || document.hidden) return
  polling = true
  controller?.abort()
  controller = new AbortController()
  if (initial) loading.value = true
  error.value = ''
  try {
    await refreshLlmDebugState()
    if (!gate.value.effective) {
      traces.value = []
      detail.value = null
      return
    }
    const result = await getLlmTraces({
      ...filters,
      page: page.value,
      per_page: 50,
    }, controller.signal)
    traces.value = result.items
    facets.value = result.facets
    total.value = result.total
    totalPages.value = result.total_pages
    if (!selectedId.value && result.items.length) selectedId.value = result.items[0].id
    if (selectedId.value) {
      const after = detail.value?.trace.id === selectedId.value ? detail.value.last_sequence : 0
      const next = await getLlmTrace(selectedId.value, after, controller.signal)
      if (after && detail.value?.trace.id === selectedId.value) {
        const known = new Set(detail.value.events.map((event) => event.sequence))
        detail.value = {
          ...next,
          events: [...detail.value.events, ...next.events.filter((event) => !known.has(event.sequence))],
        }
      } else {
        detail.value = next
      }
    }
  } catch (cause) {
    if ((cause as Error).name !== 'AbortError') error.value = cause instanceof Error ? cause.message : 'Unable to load traces'
  } finally {
    loading.value = false
    polling = false
  }
}

async function selectTrace(id: string) {
  selectedId.value = id
  detail.value = null
  await refresh(true)
}

function applyFilters() {
  page.value = 1
  selectedId.value = ''
  detail.value = null
  void refresh(true)
}

function changePage(next: number) {
  page.value = next
  selectedId.value = ''
  detail.value = null
  void refresh(true)
}

function togglePause() {
  paused.value = !paused.value
  if (!paused.value) void refresh()
}

async function copyTrace() {
  if (!detail.value) return
  await navigator.clipboard.writeText(JSON.stringify(detail.value, null, 2))
  toast.add({ severity: 'success', summary: 'Trace copied', life: 1800 })
}

async function copyEvent(event: LlmTraceEvent) {
  await navigator.clipboard.writeText(JSON.stringify(event, null, 2))
  toast.add({ severity: 'success', summary: 'Event copied', life: 1800 })
}

function downloadTrace() {
  if (!detail.value) return
  const blob = new Blob([JSON.stringify(detail.value, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `llm-trace-${detail.value.trace.id}.json`
  link.click()
  URL.revokeObjectURL(url)
}

function confirmDeleteSelected() {
  const trace = selected.value
  if (!trace) return
  confirm.require({
    header: 'Delete trace?',
    message: 'This permanently removes the selected completed trace and all of its events.',
    acceptLabel: 'Delete',
    rejectLabel: 'Cancel',
    acceptClass: 'p-button-danger',
    accept: async () => {
      await deleteLlmTrace(trace.id)
      selectedId.value = ''
      detail.value = null
      await refresh(true)
      toast.add({ severity: 'success', summary: 'Trace deleted', life: 2200 })
    },
  })
}

function confirmDeleteAll() {
  confirm.require({
    header: 'Delete all completed traces?',
    message: 'Running traces are preserved. This action cannot be undone.',
    acceptLabel: 'Delete all',
    rejectLabel: 'Cancel',
    acceptClass: 'p-button-danger',
    accept: async () => {
      const result = await deleteAllLlmTraces()
      selectedId.value = ''
      detail.value = null
      await refresh(true)
      toast.add({
        severity: result.skipped_running ? 'warn' : 'success',
        summary: `${result.deleted} trace${result.deleted === 1 ? '' : 's'} deleted`,
        detail: result.skipped_running ? `${result.skipped_running} running trace(s) kept.` : undefined,
        life: 3500,
      })
    },
  })
}

function visibilityChanged() {
  if (!document.hidden && !paused.value) void refresh()
}

onMounted(async () => {
  document.addEventListener('visibilitychange', visibilityChanged)
  await refresh(true)
  timer = setInterval(() => { void refresh() }, 1000)
})

onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
  controller?.abort()
  document.removeEventListener('visibilitychange', visibilityChanged)
})
</script>

<template>
  <section class="view-stack debug-view">
    <div class="page-heading">
      <div>
        <p>Diagnostics</p>
        <h1>LLM debug console</h1>
      </div>
      <div class="button-row">
        <Button :label="paused ? 'Resume' : 'Pause'" :icon="paused ? 'pi pi-play' : 'pi pi-pause'" severity="secondary" outlined @click="togglePause" />
        <Button label="Refresh" icon="pi pi-refresh" severity="secondary" outlined :loading="loading" @click="refresh(true)" />
        <Button label="Delete all" icon="pi pi-trash" severity="danger" outlined :disabled="!traces.length" @click="confirmDeleteAll" />
      </div>
    </div>

    <Message v-if="!gate.allowed" severity="warn" :closable="false">
      LLM tracing is disabled by the server. Set <code>LLM_DEBUG_ALLOWED=true</code> to permit it.
    </Message>
    <Message v-else-if="!gate.effective" severity="secondary" :closable="false">
      LLM tracing is off. <RouterLink to="/settings">Enable it in Settings</RouterLink> to capture new requests and view saved traces.
    </Message>
    <Message v-if="paused && gate.effective" severity="info" :closable="false">Inspector polling is paused. Capture continues on the server.</Message>
    <Message v-if="error" severity="error" :closable="false">{{ error }}</Message>

    <template v-if="gate.effective">
      <div class="debug-filters glass-panel">
        <Select v-model="filters.status" :options="statusOptions" option-label="label" option-value="value" show-clear placeholder="Status" @change="applyFilters" />
        <Select v-model="filters.operation" :options="operationOptions" option-label="label" option-value="value" show-clear placeholder="Operation" @change="applyFilters" />
        <Select v-model="filters.origin" :options="originOptions" option-label="label" option-value="value" show-clear placeholder="Origin" @change="applyFilters" />
        <Select v-model="filters.provider" :options="providerOptions" option-label="label" option-value="value" show-clear placeholder="Provider" @change="applyFilters" />
        <Select v-model="filters.model" :options="modelOptions" option-label="label" option-value="value" show-clear placeholder="Model" @change="applyFilters" />
        <InputText v-model="filters.correlation" placeholder="Exact trace, job, session, or target ID" @keyup.enter="applyFilters" />
        <Button label="Apply" icon="pi pi-filter" @click="applyFilters" />
      </div>

      <div class="debug-console glass-panel">
        <aside class="debug-timeline" aria-label="LLM traces">
          <div class="debug-pane-heading"><strong>{{ total.toLocaleString() }} traces</strong><span>Newest first</span></div>
          <button
            v-for="trace in traces"
            :key="trace.id"
            type="button"
            class="debug-trace-row"
            :class="{ 'is-selected': trace.id === selectedId }"
            @click="selectTrace(trace.id)"
          >
            <span class="debug-row-top">
              <span class="debug-status" :class="`is-${trace.status}`">{{ trace.status }}</span>
              <strong>{{ friendly(trace.operation) }}</strong>
            </span>
            <span>{{ friendly(trace.origin) }} · {{ trace.provider || 'provider pending' }}</span>
            <span class="debug-muted">{{ trace.model || 'model pending' }}</span>
            <span class="debug-row-meta">{{ formatDate(trace.started_at) }} · {{ formatDuration(trace) }} · {{ trace.attempt_count }} attempt(s)</span>
            <span v-if="trace.target_id" class="debug-id">{{ trace.target_kind }}: {{ trace.target_id }}</span>
          </button>
          <div v-if="!traces.length && !loading" class="debug-empty">No traces match these filters.</div>
          <div class="debug-pagination">
            <Button icon="pi pi-chevron-left" text :disabled="page <= 1" aria-label="Previous page" @click="changePage(page - 1)" />
            <span>Page {{ page }} / {{ totalPages }}</span>
            <Button icon="pi pi-chevron-right" text :disabled="page >= totalPages" aria-label="Next page" @click="changePage(page + 1)" />
          </div>
        </aside>

        <main class="debug-inspector">
          <div v-if="selected" class="debug-trace-header">
            <div>
              <div class="debug-row-top">
                <span class="debug-status" :class="`is-${selected.status}`">{{ selected.status }}</span>
                <h2>{{ friendly(selected.operation) }}</h2>
              </div>
              <p>{{ selected.provider || '—' }} · {{ selected.model || '—' }} · {{ selected.attempt_count }} attempt(s) · {{ selected.total_tokens ?? '—' }} tokens</p>
              <code>{{ selected.id }}</code>
            </div>
            <div class="button-row">
              <Button icon="pi pi-copy" label="Copy trace" size="small" severity="secondary" outlined @click="copyTrace" />
              <Button icon="pi pi-download" label="Download" size="small" severity="secondary" outlined @click="downloadTrace" />
              <Button icon="pi pi-trash" label="Delete" size="small" severity="danger" outlined :disabled="selected.status === 'running'" @click="confirmDeleteSelected" />
            </div>
          </div>
          <div v-if="detail" class="debug-event-rail">
            <article v-for="event in detail.events" :key="event.id" class="debug-event" :class="eventTone(event.kind)">
              <div class="debug-event-heading">
                <span class="debug-event-sequence">#{{ event.sequence }}</span>
                <strong>{{ friendly(event.kind) }}</strong>
                <span v-if="event.stage">{{ event.stage }}</span>
                <span v-if="event.attempt_number">Attempt {{ event.attempt_number }}</span>
                <time>{{ formatDate(event.created_at) }}</time>
                <Button icon="pi pi-copy" text rounded size="small" aria-label="Copy event" @click="copyEvent(event)" />
              </div>
              <JsonInspector :value="event.payload" />
            </article>
            <div v-if="detail.status === 'running'" class="debug-live"><span /> Live trace — waiting for events</div>
          </div>
          <div v-else-if="loading" class="debug-empty">Loading trace…</div>
          <div v-else class="debug-empty">Select a trace to inspect its events.</div>
        </main>
      </div>
    </template>
  </section>
</template>
