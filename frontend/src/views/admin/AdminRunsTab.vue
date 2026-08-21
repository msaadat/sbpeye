<script setup lang="ts">
/**
 * What has run against the corpus, newest first.
 *
 * Circular and laws syncs share one table on the server and are told apart by `kind`.
 * They are shown together here on purpose: the sidebar banner needs to isolate circular
 * runs, but someone asking "what happened to this corpus" wants one timeline.
 *
 * Runs still *start* from the CLI (or, for circular sync, the sidebar button). This tab
 * only reports.
 */
import { computed, onMounted, ref } from 'vue'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Column from 'primevue/column'
import DataTable from 'primevue/datatable'
import Message from 'primevue/message'
import ProgressSpinner from 'primevue/progressspinner'

import AdminStatusChip from '@/components/AdminStatusChip.vue'
import { getAdminRunHistory, type AdminAiJob, type AdminRunHistory, type AdminSyncRun } from '@/lib/api'
import { ABSENT, formatCount, formatDate, formatDuration, humanize } from './adminFormat'

const history = ref<AdminRunHistory | null>(null)
const loading = ref(false)
const error = ref('')

const syncRuns = computed(() => history.value?.sync_runs ?? [])
const aiJobs = computed(() => history.value?.ai_jobs ?? [])
const activeRuns = computed(() =>
  syncRuns.value.filter((run) => run.status === 'running' || run.status === 'queued'),
)

/**
 * A sync run's parameters, as a short readable line.
 *
 * Stored as a JSON string on the row. Only the flags that were actually set are shown —
 * the full option set is fifteen keys of mostly defaults, and printing them all buries
 * the one that explains why a run behaved unusually.
 */
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
    if (value === null || value === false || value === 0 || value === '' || key === 'kind') continue
    // An unset multi-value option is stored as `[]`, which is truthy — without this it
    // renders as a bare "Doc types:" with nothing after it on every unfiltered laws run.
    if (Array.isArray(value) && value.length === 0) continue
    parts.push(value === true ? humanize(key) : `${humanize(key)}: ${Array.isArray(value) ? value.join(', ') : value}`)
  }
  return parts.length ? parts.join(' · ') : 'defaults'
}

function counts(run: AdminSyncRun): string {
  const parts: string[] = []
  if (run.processed_count !== null && run.processed_count !== undefined) {
    parts.push(`${formatCount(run.processed_count)} processed`)
  }
  if (run.skipped_count) parts.push(`${formatCount(run.skipped_count)} skipped`)
  if (run.error_count) parts.push(`${formatCount(run.error_count)} errors`)
  return parts.join(' · ') || ABSENT
}

function jobProgress(job: AdminAiJob): string {
  if (!job.progress_total) return ABSENT
  return `${formatCount(job.progress_completed)} / ${formatCount(job.progress_total)}`
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    history.value = await getAdminRunHistory(50)
  } catch (err) {
    error.value = (err as Error).message || 'Could not load run history.'
  } finally {
    loading.value = false
  }
}

onMounted(load)
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

    <div v-if="loading && !history" class="tab-loading">
      <ProgressSpinner style="width: 2rem; height: 2rem" />
    </div>

    <template v-else-if="history">
      <Message v-if="activeRuns.length" severity="info" :closable="false">
        {{ activeRuns.length }} run{{ activeRuns.length === 1 ? '' : 's' }} in flight.
        Refresh to follow progress.
      </Message>

      <Card class="glass-panel">
        <template #title>Sync runs</template>
        <template #content>
          <DataTable :value="syncRuns" size="small" class="facet-table" :loading="loading">
            <Column header="Corpus" style="width: 7rem">
              <template #body="{ data }">{{ humanize(data.kind) }}</template>
            </Column>
            <Column header="Status" style="width: 9rem">
              <template #body="{ data }">
                <AdminStatusChip :status="data.status" />
              </template>
            </Column>
            <Column header="Started">
              <template #body="{ data }">{{ formatDate(data.started_at) }}</template>
            </Column>
            <Column header="Took" style="width: 7rem">
              <template #body="{ data }">{{ formatDuration(data.duration_seconds) }}</template>
            </Column>
            <Column header="Result">
              <template #body="{ data }">
                <span>{{ counts(data) }}</span>
                <span v-if="data.error" class="row-error" :title="data.error">{{ data.error }}</span>
              </template>
            </Column>
            <Column header="Options">
              <template #body="{ data }">
                <span class="muted-text">{{ describeParameters(data.parameters) }}</span>
              </template>
            </Column>
            <template #empty>Nothing has been synced yet.</template>
          </DataTable>
          <p class="field-hint">
            Both corpora share this table. A run left mid-flight by a stopped process is
            released as failed on the next server start, so nothing stays "running" for ever.
          </p>
        </template>
      </Card>

      <Card class="glass-panel">
        <template #title>AI generation jobs</template>
        <template #content>
          <DataTable :value="aiJobs" size="small" class="facet-table" :loading="loading">
            <Column header="Target">
              <template #body="{ data }">
                <span>{{ data.target_label || data.target_id || ABSENT }}</span>
                <span class="muted-text"> · {{ humanize(data.target_kind) }}</span>
              </template>
            </Column>
            <Column header="Feature" style="width: 9rem">
              <template #body="{ data }">{{ humanize(data.feature) }}</template>
            </Column>
            <Column header="Status" style="width: 9rem">
              <template #body="{ data }">
                <AdminStatusChip :status="data.result_status || data.status" />
              </template>
            </Column>
            <Column header="Progress" style="width: 7rem">
              <template #body="{ data }">{{ jobProgress(data) }}</template>
            </Column>
            <Column header="Started">
              <template #body="{ data }">{{ formatDate(data.started_at || data.created_at) }}</template>
            </Column>
            <Column header="">
              <template #body="{ data }">
                <span v-if="data.error" class="row-error" :title="data.error">{{ data.error }}</span>
              </template>
            </Column>
            <template #empty>No generation jobs yet.</template>
          </DataTable>
          <p class="field-hint">
            Queued from a circular or law page, one document at a time. Corpus-wide batches
            are still CLI commands — <span class="mono">sbpeye circulars summarize</span>
            and friends.
          </p>
        </template>
      </Card>
    </template>
  </div>
</template>
