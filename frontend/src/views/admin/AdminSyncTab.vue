<script setup lang="ts">
/**
 * The corpus writes an administrator can trigger, and the one check worth running first.
 *
 * This tab exists because the deployment can now reach SBP. While it could not, corpus
 * updates happened on a maintainer's machine and the result was re-uploaded, so the
 * console reported and never wrote. That constraint is gone (deployment plan 2.1), and
 * what replaces it is this: sync runs *here*, against the volume, in the same process
 * that is serving users.
 *
 * The routes are the ones that already existed in `main.py` — this tab calls across to
 * them rather than growing its own. `api/admin.py` stays read-only, which is what lets
 * its `/index/audit` reconciler advertise `write=False` and be believed.
 *
 * Reachability sits above the sync controls rather than under them on purpose. It is the
 * question that decides whether pressing Start does anything at all, and answering it
 * costs ~2s per attempt against SBP, so it is a button and not a page load.
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Column from 'primevue/column'
import DataTable from 'primevue/datatable'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import ProgressSpinner from 'primevue/progressspinner'
import ToggleSwitch from 'primevue/toggleswitch'

import AdminStatusChip from '@/components/AdminStatusChip.vue'
import {
  getAdminEnvironment,
  getAppStatus,
  getCircularSyncStatus,
  refreshEcoDataIndex,
  runSbpReachability,
  startCircularSync,
  type AdminEnvironment,
  type ApiError,
  type AppStatus,
  type CircularSyncStatus,
  type SbpReachability,
} from '@/lib/api'
import { ABSENT, formatCount, formatDate } from './adminFormat'

const toast = useToast()

const sync = ref<CircularSyncStatus | null>(null)
const appStatus = ref<AppStatus | null>(null)
const environment = ref<AdminEnvironment | null>(null)
const loadError = ref('')
const loading = ref(false)
const starting = ref(false)

const running = computed(() => !!sync.value?.running)

/* ------------------------------------------------------------------ sync options
 *
 * Defaults match what `POST /api/circulars/sync` applies to an empty body, so pressing
 * Start with nothing filled in does exactly what the sidebar button does. Everything
 * here is the considered case: a backfill, a re-fetch, one department.
 */
const departments = ref('')
const years = ref('')
const limit = ref(0)
const workers = ref(1)
const includeAttachments = ref(true)
const forceFetch = ref(false)
const forceDownload = ref(false)
const fullListing = ref(false)
const showOptions = ref(false)

const optionsChanged = computed(
  () =>
    departments.value.trim() !== '' ||
    years.value.trim() !== '' ||
    (limit.value || 0) > 0 ||
    (workers.value || 1) !== 1 ||
    !includeAttachments.value ||
    forceFetch.value ||
    forceDownload.value ||
    fullListing.value,
)

/** How many circulars SBP is listing that this corpus does not hold. */
const newOnSbp = computed(() => appStatus.value?.remote_new_count ?? null)

/**
 * The scheduler's interval, as the container sees it.
 *
 * Surfaced here rather than only on the Deployment tab because it is the other writer:
 * an unset value is not "off", it is the 3600s default, and that distinction is the
 * difference between a corpus nothing touches and one being rewritten hourly.
 */
const schedulerInterval = computed(() => {
  const raw = environment.value?.capabilities?.ecodata_refresh_seconds
  if (raw === null || raw === undefined || raw === '') return null
  const parsed = Number(raw)
  return Number.isFinite(parsed) ? parsed : null
})

const schedulerLabel = computed(() => {
  if (!environment.value) return ABSENT
  const seconds = schedulerInterval.value
  if (seconds === null) return 'Unset — running at the 3600s default'
  if (seconds <= 0) return 'Disabled'
  return `Every ${seconds}s`
})

let pollTimer: ReturnType<typeof setInterval> | null = null

function stopPolling() {
  if (pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

/**
 * Poll only while something is in flight.
 *
 * There is no progress on the wire — `SyncStatus` gets its counts written once, when the
 * run finishes — so this is watching for the terminal state rather than following a bar.
 * Four seconds is slow enough to cost nothing and fast enough that a short incremental
 * sync does not appear to hang after it has already finished.
 */
function syncPolling() {
  if (running.value && pollTimer === null) {
    pollTimer = setInterval(() => void refreshStatus(), 4000)
  } else if (!running.value) {
    stopPolling()
  }
}

async function refreshStatus(): Promise<void> {
  try {
    sync.value = await getCircularSyncStatus()
  } catch (error) {
    loadError.value = (error as Error).message
  } finally {
    syncPolling()
  }
}

async function load(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    // `/app/status` carries the remote-availability check, which is cached server-side
    // for 30 minutes — cheap to ask for alongside the rest.
    const [syncStatus, status] = await Promise.all([getCircularSyncStatus(), getAppStatus()])
    sync.value = syncStatus
    appStatus.value = status
  } catch (error) {
    loadError.value = (error as Error).message || 'Could not read sync status.'
  } finally {
    loading.value = false
    syncPolling()
  }
  try {
    environment.value = await getAdminEnvironment()
  } catch {
    // The environment card is context, not the point of the tab. A failure here must not
    // hide the sync controls, so it degrades to the ABSENT dash.
    environment.value = null
  }
}

async function start(): Promise<void> {
  if (running.value || starting.value) return
  starting.value = true
  try {
    sync.value = await startCircularSync({
      departments: departments.value.trim() || null,
      years: years.value.trim() || null,
      limit: limit.value || 0,
      workers: workers.value || 1,
      include_attachments: includeAttachments.value,
      force_fetch: forceFetch.value,
      force_download: forceDownload.value,
      full_listing: fullListing.value,
    })
    toast.add({
      severity: 'success',
      summary: 'Sync started',
      detail: 'Running on the server. The app stays available while it works.',
      life: 3500,
    })
    syncPolling()
  } catch (error) {
    const apiError = error as ApiError
    toast.add({
      severity: apiError.status === 409 ? 'warn' : 'error',
      summary: apiError.status === 409 ? 'A sync is already running' : 'Sync could not start',
      detail: apiError.message,
      life: 5000,
    })
    void refreshStatus()
  } finally {
    starting.value = false
  }
}

/* ------------------------------------------------------------------ ecodata */

const ecoRefreshing = ref(false)

async function refreshEcoData(): Promise<void> {
  ecoRefreshing.value = true
  try {
    const result = await refreshEcoDataIndex()
    toast.add({
      severity: 'success',
      summary: 'EcoData index refreshed',
      detail: `${formatCount(result.entries)} entries now listed.`,
      life: 3500,
    })
  } catch (error) {
    const apiError = error as ApiError
    toast.add({
      severity: apiError.status === 409 ? 'warn' : 'error',
      summary: apiError.status === 409 ? 'A refresh is already running' : 'Refresh failed',
      detail: apiError.message,
      life: 5000,
    })
  } finally {
    ecoRefreshing.value = false
  }
}

/* ------------------------------------------------------------------ reachability */

const probeAttempts = ref(3)
const probe = ref<SbpReachability | null>(null)
const probeRunning = ref(false)
const probeError = ref('')

/** Roughly what the probe will cost, so the wait is a choice rather than a surprise. */
const probeCost = computed(() => Math.round((probeAttempts.value || 1) * 2 * 2.5))

async function runProbe(): Promise<void> {
  probeRunning.value = true
  probeError.value = ''
  try {
    probe.value = await runSbpReachability(probeAttempts.value || 3)
  } catch (error) {
    const apiError = error as ApiError
    probeError.value =
      apiError.status === 409
        ? 'Another probe is already running; try again shortly.'
        : apiError.message || 'The probe could not run.'
  } finally {
    probeRunning.value = false
  }
}

function cellRate(cell: SbpReachability['summary'][number]): string {
  return `${cell.ok}/${cell.attempts}`
}

onMounted(load)
onUnmounted(stopPolling)
</script>

<template>
  <div class="admin-tab-body">
    <div class="tab-toolbar">
      <span v-if="sync" class="muted-text">
        Last successful sync {{ sync.last_sync_display || ABSENT }}
      </span>
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

    <Message v-if="loadError" severity="error" :closable="false">{{ loadError }}</Message>

    <div v-if="loading && !sync" class="tab-loading">
      <ProgressSpinner style="width: 2rem; height: 2rem" />
    </div>

    <template v-else>
      <Card class="glass-panel">
        <template #title>Can this server reach SBP?</template>
        <template #content>
          <p class="field-hint">
            Every control below depends on the answer, and the answer is not a setting —
            some requests get through and some do not. This runs the probe from the
            container, which is the only address SBP's edge actually sees.
          </p>

          <div class="probe-controls">
            <label class="probe-field">
              <span>Attempts per cell</span>
              <InputNumber v-model="probeAttempts" :min="1" :max="20" show-buttons size="small" />
            </label>
            <Button
              label="Run probe"
              icon="pi pi-bolt"
              size="small"
              outlined
              :loading="probeRunning"
              @click="runProbe"
            />
            <span class="muted-text">~{{ probeCost }}s, serial. Writes nothing.</span>
          </div>

          <Message v-if="probeError" severity="error" :closable="false">{{ probeError }}</Message>

          <template v-if="probe">
            <dl class="kv-list">
              <dt>Verdict</dt>
              <dd><AdminStatusChip :status="probe.verdict" /></dd>
              <dt>Egress IP</dt>
              <dd class="mono">
                {{ probe.egress.ok ? probe.egress.ip : probe.egress.error || ABSENT }}
              </dd>
              <dt>Took</dt>
              <dd>{{ probe.duration_s }}s</dd>
            </dl>

            <DataTable :value="probe.summary" size="small" class="facet-table">
              <Column field="target" header="Target" />
              <Column field="arm" header="Client" />
              <Column header="OK">
                <template #body="{ data }">{{ cellRate(data) }}</template>
              </Column>
              <Column field="forbidden" header="403" />
              <Column field="challenged" header="Challenge" />
              <Column field="errors" header="Errors" />
              <Column header="Median">
                <template #body="{ data }">
                  {{ data.median_ms === null ? ABSENT : `${data.median_ms}ms` }}
                </template>
              </Column>
            </DataTable>
            <p v-if="probe.skipped_arms.length" class="field-hint">
              Skipped: {{ probe.skipped_arms.join(', ') }}
            </p>
          </template>
        </template>
      </Card>

      <Card class="glass-panel">
        <template #title>Circular sync</template>
        <template #content>
          <dl class="kv-list">
            <dt>State</dt>
            <dd><AdminStatusChip :status="sync?.status || 'idle'" /></dd>
            <dt>Started</dt>
            <dd>{{ formatDate(sync?.started_at) }}</dd>
            <dt>Finished</dt>
            <dd>{{ formatDate(sync?.completed_at) }}</dd>
            <dt>Last result</dt>
            <dd>
              <span v-if="sync?.processed_count !== null && sync?.processed_count !== undefined">
                {{ formatCount(sync.processed_count) }} processed ·
                {{ formatCount(sync.skipped_count) }} skipped ·
                {{ formatCount(sync.error_count) }} errors
              </span>
              <span v-else>{{ ABSENT }}</span>
            </dd>
            <dt>New on SBP</dt>
            <dd>
              <span v-if="newOnSbp !== null">
                {{ formatCount(newOnSbp) }} not in this corpus
              </span>
              <span v-else class="muted-text">Not checked</span>
            </dd>
          </dl>

          <Message v-if="sync?.error" severity="error" :closable="false">{{ sync.error }}</Message>

          <Message v-if="running" severity="info" :closable="false">
            Sync is running on the server. Counts are written when it finishes, so this
            shows a state rather than a progress bar. Leaving the page does not stop it.
          </Message>

          <div class="sync-actions">
            <Button
              :label="running ? 'Sync running' : 'Start sync'"
              icon="pi pi-cloud-download"
              :loading="starting"
              :disabled="running"
              @click="start"
            />
            <Button
              :label="showOptions ? 'Hide options' : 'Options'"
              :icon="showOptions ? 'pi pi-chevron-up' : 'pi pi-sliders-h'"
              severity="secondary"
              text
              size="small"
              @click="showOptions = !showOptions"
            />
            <span v-if="optionsChanged && !showOptions" class="muted-text">
              Options are not at their defaults.
            </span>
          </div>

          <div v-if="showOptions" class="sync-options">
            <label class="sync-field">
              <span>Departments</span>
              <InputText v-model="departments" placeholder="Comma-separated, blank for all" />
            </label>
            <label class="sync-field">
              <span>Years</span>
              <InputText v-model="years" placeholder="e.g. 2025, 2026" />
            </label>
            <label class="sync-field">
              <span>Limit</span>
              <InputNumber v-model="limit" :min="0" show-buttons />
            </label>
            <label class="sync-field">
              <span>Workers</span>
              <InputNumber v-model="workers" :min="1" :max="8" show-buttons />
            </label>

            <label class="inline-toggle">
              <ToggleSwitch v-model="includeAttachments" />
              <span>Download attachments</span>
            </label>
            <label class="inline-toggle">
              <ToggleSwitch v-model="fullListing" />
              <span>Walk the full listing</span>
            </label>
            <label class="inline-toggle">
              <ToggleSwitch v-model="forceFetch" />
              <span>Re-fetch pages already held</span>
            </label>
            <label class="inline-toggle">
              <ToggleSwitch v-model="forceDownload" />
              <span>Re-download files already held</span>
            </label>

            <p class="field-hint">
              Workers are concurrent writers against the same database this deployment is
              serving from. Two or three shortens a backfill without much risk; eight is
              for a machine with no users on it. Left at one, and with everything else at
              its default, this is the same incremental run as the sidebar button —
              newest circulars only, stopping at the latest date the corpus already holds.
            </p>
            <p class="field-hint">
              <strong>Walk the full listing</strong> ignores that stopping point and reads
              every page, which is what a backfill of older years needs.
              <strong>Re-download</strong> refetches files the corpus already has a copy
              of — the way to repair a volume whose ledger and disk disagree.
            </p>
          </div>
        </template>
      </Card>

      <Card class="glass-panel">
        <template #title>EcoData index</template>
        <template #content>
          <dl class="kv-list">
            <dt>Scheduled refresh</dt>
            <dd>{{ schedulerLabel }}</dd>
          </dl>
          <p class="field-hint">
            A re-scrape of SBP's economic-data index: entry rows only, no files and no
            vectors. It replaces the table wholesale rather than merging, so a refresh
            against a partial page leaves a partial index until the next good one.
          </p>
          <Button
            label="Refresh now"
            icon="pi pi-sync"
            severity="secondary"
            outlined
            :loading="ecoRefreshing"
            @click="refreshEcoData"
          />
        </template>
      </Card>
    </template>
  </div>
</template>

<style scoped>
.probe-controls,
.sync-actions {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  flex-wrap: wrap;
  margin: 0.75rem 0;
}

.probe-field {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  font-size: var(--sbp-fs-meta);
}

.probe-field :deep(.p-inputnumber-input) {
  width: 4.5rem;
}

.sync-options {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr));
  gap: 0.75rem 1.25rem;
  padding: 0.9rem 0 0;
  border-top: 1px solid var(--sbp-border);
  margin-top: 0.75rem;
}

.sync-field {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  font-size: var(--sbp-fs-meta);
}

.inline-toggle {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  font-size: var(--sbp-fs-meta);
}

/* The hints explain the whole options block, so they span it rather than sitting in a
   column of their own and wrapping to four words a line. */
.sync-options .field-hint {
  grid-column: 1 / -1;
  margin: 0;
}
</style>
