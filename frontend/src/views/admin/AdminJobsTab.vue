<script setup lang="ts">
/**
 * Everything this server has run against the corpus, newest first.
 *
 * There were two of these: a "Jobs" tab listing maintenance batches, and an "All run
 * history" tab in the console's second row listing mirror audits, sync runs and AI
 * generation jobs in three separate cards. They answer one question — what has this
 * server been doing — and an operator asking it had to know which of the two tabs owned
 * which half of the answer, then read four lists in two places to assemble a timeline.
 *
 * So: one table, sorted by start time across all four kinds, with a type switch. The
 * kinds do not share a backing table, which is why the row shape is built here rather
 * than served: `JobRow` is the least that lets four unlike records sit in one column set
 * without either of them lying about what it is.
 *
 * Maintenance batches are the only kind this console can act on. Retry and cancel appear
 * on those rows alone; the rest link to the tab that owns them.
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Column from 'primevue/column'
import DataTable from 'primevue/datatable'
import Message from 'primevue/message'
import ProgressSpinner from 'primevue/progressspinner'

import AdminStatusChip from '@/components/AdminStatusChip.vue'
import {
  getAdminRunHistory, requestJson,
  type AdminAiJob, type AdminRunHistory, type AdminSyncRun,
} from '@/lib/api'
import { ABSENT, formatCount, formatDate, formatDuration, humanize, statusTone } from './adminFormat'

type JobKind = 'maintenance' | 'mirror' | 'sync' | 'ai'

interface MaintenanceJob {
  job_id: string; status: string; started_at: string; error?: string
  parameters: { action?: string; operation: string; features?: string[] }
  progress: { items?: { id: string; feature?: string; status: string; error?: string }[]; cancel_requested?: boolean }
}

interface JobRow {
  key: string
  kind: JobKind
  kindLabel: string
  startedAt: string | null
  target: string
  status: string
  progress: string
  duration: number | null
  error?: string | null
  /** Present only for maintenance batches — the one kind with controls here. */
  job?: MaintenanceJob
  link?: { label: string; to: string }
}

const KINDS: Array<{ value: JobKind | 'all'; label: string }> = [
  { value: 'all', label: 'All' },
  { value: 'maintenance', label: 'Maintenance' },
  { value: 'mirror', label: 'Mirror audits' },
  { value: 'sync', label: 'Sync runs' },
  { value: 'ai', label: 'AI generation' },
]

const maintenance = ref<MaintenanceJob[]>([])
const history = ref<AdminRunHistory | null>(null)
const kind = ref<JobKind | 'all'>('all')
const opened = ref<MaintenanceJob | null>(null)
const loading = ref(false)
const submitting = ref(false)
const error = ref('')

let timer: ReturnType<typeof setTimeout> | undefined
let disposed = false

const isActive = (status: string) => ['queued', 'running'].includes(status)

/** A sync run's parameters, as a short readable line.
 *
 * Stored as a JSON string on the row. Only the flags that were actually set are shown —
 * the full option set is fifteen keys of mostly defaults, and printing them all buries
 * the one that explains why a run behaved unusually. */
function describeParameters(raw?: string | null): string {
  if (!raw) return ABSENT
  let parsed: Record<string, unknown>
  try {
    parsed = JSON.parse(raw)
  } catch {
    return raw.slice(0, 80)
  }
  const parts: string[] = []
  for (const [key, value] of Object.entries(parsed)) {
    // Sync's analysis outcome is shown by the sync status, not as a request option.
    if (key === 'generation_result') continue
    if (value === null || value === false || value === 0 || value === '' || key === 'kind') continue
    // An unset multi-value option is stored as `[]`, which is truthy — without this it
    // renders as a bare "Doc types:" with nothing after it on every unfiltered laws run.
    if (Array.isArray(value) && value.length === 0) continue
    parts.push(value === true ? humanize(key) : `${humanize(key)}: ${Array.isArray(value) ? value.join(', ') : value}`)
  }
  return parts.length ? parts.join(' · ') : 'defaults'
}

function syncCounts(run: AdminSyncRun): string {
  const parts: string[] = []
  if (run.processed_count !== null && run.processed_count !== undefined) {
    parts.push(`${formatCount(run.processed_count)} processed`)
  }
  if (run.skipped_count) parts.push(`${formatCount(run.skipped_count)} skipped`)
  if (run.error_count) parts.push(`${formatCount(run.error_count)} errors`)
  return parts.join(' · ') || ABSENT
}

/** Elapsed seconds between two timestamps, or null when either is missing. */
function elapsed(from?: string | null, to?: string | null): number | null {
  if (!from || !to) return null
  const started = new Date(from).getTime()
  const finished = new Date(to).getTime()
  if (Number.isNaN(started) || Number.isNaN(finished)) return null
  return Math.max(0, (finished - started) / 1000)
}

function maintenanceRow(job: MaintenanceJob): JobRow {
  const items = job.progress.items ?? []
  const done = items.filter((item) => item.status === 'completed').length
  const errors = items.filter((item) => item.status === 'failed').length
  const action = job.parameters.action || 'backfill'
  const features = job.parameters.features?.length ? ` · ${job.parameters.features.join(', ')}` : ''
  return {
    key: `maintenance:${job.job_id}`,
    kind: 'maintenance',
    kindLabel: 'Maintenance',
    startedAt: job.started_at,
    target: `${humanize(action)}${features}`,
    status: job.status,
    progress: items.length
      ? `${formatCount(done)} / ${formatCount(items.length)}${errors ? ` · ${formatCount(errors)} errors` : ''}`
      : ABSENT,
    duration: null,
    error: job.error,
    job,
    link: {
      label: 'Audit this batch',
      to: `/admin/documents/workbench?scope=attachments&finding=actionable&source_job_id=${encodeURIComponent(job.job_id)}`,
    },
  }
}

function aiRow(job: AdminAiJob): JobRow {
  return {
    key: `ai:${job.id}`,
    kind: 'ai',
    kindLabel: 'AI generation',
    startedAt: job.started_at || job.created_at || null,
    target: `${job.target_label || job.target_id || ABSENT} · ${humanize(job.target_kind)}`,
    status: job.result_status || job.status,
    progress: job.progress_total ? `${formatCount(job.progress_completed)} / ${formatCount(job.progress_total)}` : ABSENT,
    duration: elapsed(job.started_at, job.completed_at),
    error: job.error,
  }
}

const rows = computed<JobRow[]>(() => {
  const all: JobRow[] = [
    ...maintenance.value.map(maintenanceRow),
    ...(history.value?.mirror_audits ?? []).map((audit) => ({
      key: `mirror:${audit.id}`,
      kind: 'mirror' as const,
      kindLabel: 'Mirror audit',
      startedAt: audit.started_at,
      target: `Full listing · ${formatCount(audit.distinct_total)} identities`,
      status: audit.status,
      progress: `${formatCount(audit.counts?.missing ?? 0)} missing · ${formatCount(audit.counts?.ambiguous ?? 0)} ambiguous`,
      duration: null,
      error: audit.error,
      link: { label: 'Findings', to: '/admin/ingest/audit' },
    })),
    ...(history.value?.sync_runs ?? []).map((run) => ({
      key: `sync:${run.id}`,
      kind: 'sync' as const,
      kindLabel: 'Sync run',
      startedAt: run.started_at ?? null,
      target: `${humanize(run.kind)} · ${describeParameters(run.parameters)}`,
      status: run.status,
      progress: syncCounts(run),
      duration: run.duration_seconds ?? null,
      error: run.error,
      link: { label: 'Source', to: '/admin/ingest/source' },
    })),
    ...(history.value?.ai_jobs ?? []).map(aiRow),
  ]
  return all.sort((left, right) => {
    const a = left.startedAt ? new Date(left.startedAt).getTime() : 0
    const b = right.startedAt ? new Date(right.startedAt).getTime() : 0
    return b - a
  })
})

const visible = computed(() => (kind.value === 'all' ? rows.value : rows.value.filter((row) => row.kind === kind.value)))
const running = computed(() => rows.value.filter((row) => isActive(row.status)))
const counts = computed(() => {
  const tally: Record<string, number> = { all: rows.value.length }
  for (const row of rows.value) tally[row.kind] = (tally[row.kind] || 0) + 1
  return tally
})

const openedItems = computed(() => opened.value?.progress.items ?? [])

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  const [batches, runs] = await Promise.allSettled([
    requestJson<MaintenanceJob[]>('/admin/maintenance/jobs'),
    getAdminRunHistory(50),
  ])
  if (disposed) return
  if (batches.status === 'fulfilled') {
    maintenance.value = batches.value
    if (opened.value) opened.value = batches.value.find((job) => job.job_id === opened.value?.job_id) || opened.value
  } else {
    error.value = (batches.reason as Error)?.message || 'Could not read maintenance batches.'
  }
  if (runs.status === 'fulfilled') history.value = runs.value
  else if (!error.value) error.value = (runs.reason as Error)?.message || 'Could not read run history.'
  loading.value = false
  schedule()
}

/* Jobs are the one screen where standing still is wrong: a running batch that reaches
   the end of its work should say so without the operator pressing anything. */
function schedule(): void {
  clearTimeout(timer)
  if (!disposed && running.value.length) timer = setTimeout(() => void load(), 5000)
}

async function post(path: string): Promise<void> {
  submitting.value = true
  try {
    await requestJson(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
    await load()
  } catch (exc) {
    error.value = String(exc)
  } finally {
    submitting.value = false
  }
}

function cancel(job: MaintenanceJob): void {
  void post(`/circulars/maintenance/jobs/${job.job_id}/cancel`)
}

async function retry(job: MaintenanceJob): Promise<void> {
  const ids = (job.progress.items ?? []).filter((item) => item.status !== 'completed').map((item) => item.id)
  if (!ids.length || !job.parameters.action) return
  submitting.value = true
  try {
    await requestJson('/circulars/maintenance/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids, action: job.parameters.action, features: job.parameters.features || [], size: ids.length }),
    })
    await load()
  } catch (exc) {
    error.value = String(exc)
  } finally {
    submitting.value = false
  }
}

onMounted(load)
onUnmounted(() => { disposed = true; clearTimeout(timer) })
</script>

<template>
  <div class="admin-tab-body">
    <div class="tab-toolbar">
      <span v-if="history" class="muted-text">Read {{ formatDate(history.generated_at) }}</span>
      <Button
        label="Refresh"
        icon="pi pi-refresh"
        severity="secondary"
        outlined
        size="small"
        :loading="loading"
        @click="load"
      />
    </div>

    <Message v-if="error" severity="error" :closable="false">{{ error }}</Message>

    <div v-if="loading && !rows.length" class="tab-loading">
      <ProgressSpinner style="width: 2rem; height: 2rem" />
    </div>

    <template v-else>
      <div class="switch-row">
        <div class="segmented-switch" role="group" aria-label="Job type">
          <button
            v-for="entry in KINDS"
            :key="entry.value"
            type="button"
            :aria-pressed="kind === entry.value"
            @click="kind = entry.value"
          >
            {{ entry.label }}
            <span class="muted-text">{{ formatCount(counts[entry.value] ?? 0) }}</span>
          </button>
        </div>
        <span v-if="running.length" class="muted-text">
          {{ running.length }} in flight · refreshing every 5s
        </span>
      </div>

      <DataTable
        :value="visible"
        data-key="key"
        size="small"
        class="facet-table"
        paginator
        :rows="25"
        :loading="loading"
      >
        <Column header="Started" style="width: 12rem">
          <template #body="{ data }">{{ formatDate(data.startedAt) }}</template>
        </Column>
        <Column header="Type" style="width: 9rem">
          <template #body="{ data }">{{ data.kindLabel }}</template>
        </Column>
        <Column header="Target" style="min-width: 16rem">
          <template #body="{ data }">
            <span>{{ data.target }}</span>
            <span v-if="data.error" class="row-error" :title="data.error">{{ data.error }}</span>
          </template>
        </Column>
        <Column header="Status" style="width: 9rem">
          <template #body="{ data }">
            <AdminStatusChip :status="data.status" />
          </template>
        </Column>
        <Column header="Progress" style="min-width: 11rem">
          <template #body="{ data }">
            <span class="muted-text">{{ data.progress }}</span>
          </template>
        </Column>
        <Column header="Took" style="width: 7rem">
          <template #body="{ data }">{{ formatDuration(data.duration) }}</template>
        </Column>
        <Column header="" style="min-width: 16rem">
          <template #body="{ data }">
            <div class="row-actions">
              <Button
                v-if="data.job"
                label="Items"
                text
                size="small"
                @click="opened = opened?.job_id === data.job.job_id ? null : data.job"
              />
              <Button
                v-if="data.job && data.job.parameters.operation === 'maintenance' && !isActive(data.status)"
                label="Retry unfinished"
                text
                size="small"
                :disabled="submitting"
                @click="retry(data.job)"
              />
              <Button
                v-if="data.job && data.job.parameters.operation === 'maintenance' && isActive(data.status)"
                :label="data.job.progress.cancel_requested ? 'Cancellation requested' : 'Cancel after current item'"
                text
                size="small"
                :disabled="submitting || Boolean(data.job.progress.cancel_requested)"
                @click="cancel(data.job)"
              />
              <RouterLink v-if="data.link" :to="data.link.to" class="row-link">{{ data.link.label }}</RouterLink>
            </div>
          </template>
        </Column>
        <template #empty>Nothing has run yet.</template>
      </DataTable>

      <Card v-if="opened" class="glass-panel">
        <template #title>Batch {{ opened.job_id.slice(0, 8) }} · {{ humanize(opened.parameters.action || 'backfill') }}</template>
        <template #content>
          <dl class="kv-list">
            <dt>Status</dt>
            <dd><AdminStatusChip :status="opened.status" /></dd>
            <dt>Started</dt>
            <dd>{{ formatDate(opened.started_at) }}</dd>
            <dt>Cancellation</dt>
            <dd>{{ opened.progress.cancel_requested ? 'requested' : 'not requested' }}</dd>
            <template v-if="opened.error">
              <dt>Error</dt>
              <dd>{{ opened.error }}</dd>
            </template>
          </dl>

          <h3 class="section-label">Item outcomes</h3>
          <div v-if="!openedItems.length" class="muted-text">No items recorded yet.</div>
          <div v-else class="attn-list item-scroll">
            <div
              v-for="item in openedItems"
              :key="`${item.id}:${item.feature || ''}`"
              class="attn"
              :class="`tone-${statusTone(item.status)}`"
            >
              <span class="attn-body">
                <RouterLink :to="`/circulars/${item.id}`" class="mono">{{ item.id }}</RouterLink>
                <span v-if="item.feature" class="muted-text"> · {{ item.feature }}</span>
                <small v-if="item.error">{{ item.error }}</small>
              </span>
              <AdminStatusChip :status="item.status" />
            </div>
          </div>

          <p class="field-hint">
            Retry creates a new job for the documents this one left unfinished or failed.
            Auditing the batch opens the workbench filtered to its saved IDs, across
            departments and years.
          </p>
        </template>
      </Card>
    </template>
  </div>
</template>

<style scoped>
.row-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.3rem;
}

.row-link {
  font-size: var(--sbp-fs-meta);
  white-space: nowrap;
}

/* A batch of 500 would otherwise push the hint below it off the end of the page. */
.item-scroll {
  max-height: 22rem;
  overflow-y: auto;
}
</style>
