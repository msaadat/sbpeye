<script setup lang="ts">
/**
 * Ingest: what SBP lists, what we hold, and closing the difference.
 *
 * One component behind three routes — `/admin/ingest/audit`, `/gaps` and `/backfill` —
 * because they are three views of one piece of live state. The audit's findings, the
 * queue those findings feed, and the job draining that queue all move together, and all
 * three need the same 2-second poll while something is running. Split into three
 * components, that poll and the job it follows would exist three times over, and a
 * backfill started on one tab would be invisible on the next.
 *
 * What did change is the shape: this was four stacked cards of hand-rolled tables and
 * native selects on a page of its own stylesheet, in a console where six other tabs
 * already shared one. It now renders through the same cards, tables and chips as the
 * rest, and shows one task at a time instead of all four at once.
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import Accordion from 'primevue/accordion'
import AccordionContent from 'primevue/accordioncontent'
import AccordionHeader from 'primevue/accordionheader'
import AccordionPanel from 'primevue/accordionpanel'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Checkbox from 'primevue/checkbox'
import Column from 'primevue/column'
import DataTable from 'primevue/datatable'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import ProgressSpinner from 'primevue/progressspinner'
import Select from 'primevue/select'

import AdminStatusChip from '@/components/AdminStatusChip.vue'
import IdentityMigrationPanel from './IdentityMigrationPanel.vue'
import {
  getMirrorOverview, getMirrorGaps, getMirrorFindings, getMirrorJob, getMirrorAttempts, mirrorAction,
  type MirrorOverview, type MirrorGap, type MirrorFinding, type MirrorJob, type MirrorAttempt,
} from '@/lib/api'
import { formatDate, formatCount, humanize } from './adminFormat'

const BUCKETS = ['matched', 'drifted', 'missing', 'ambiguous', 'unlisted_local']
const QUEUE_STATUSES = ['pending', 'failed', 'resolved', 'skipped', 'running']
const FEATURES = ['summary', 'tags', 'checklist', 'relationships', 'entities', 'consolidation']

const route = useRoute()
/** `audit` | `gaps` | `backfill` — which third of the ingest state is on screen. */
const view = computed(() => route.path.split('/').pop() || 'audit')

const overview = ref<MirrorOverview | null>(null)
const maintenanceActive = ref(false)
const gaps = ref<MirrorGap[]>([])
const findings = ref<MirrorFinding[]>([])
const attempts = ref<MirrorAttempt[]>([])
const job = ref<MirrorJob | null>(null)
const error = ref('')
const notice = ref('')
const loading = ref(false)
const submitting = ref(false)
const page = ref(1)
const total = ref(0)
const findingPage = ref(1)
const findingTotal = ref(0)
const attemptPage = ref(1)
const attemptTotal = ref(0)
const status = ref('pending')
const bucket = ref('missing')
const q = ref('')
const year = ref<number | null>(null)
const department = ref('')
const size = ref(50)
const workers = ref(2)
const delay = ref(0.5)
const maxAttempts = ref(3)
const attachments = ref(false)
const repeat = ref(false)
const includeFailed = ref(false)
const order = ref('newest_first')
const selectedFeatures = ref<string[]>([])
const skipReason = ref('')
const openDetail = ref<string[]>([])

let timer: ReturnType<typeof setTimeout> | undefined
let disposed = false
let pendingReload = false

const storedJob = ref(localStorage.getItem('sbpeye-mirror-job') || '')
const active = (value?: string) => value === 'running' || value === 'queued'
const busy = computed(() => submitting.value
  || active(overview.value?.audit?.status)
  || active(overview.value?.active_job?.status)
  || active(job.value?.status))
const baseline = computed(() => overview.value?.latest_complete_audit)

const query = computed(() => {
  const params = new URLSearchParams({ page: String(page.value), per_page: '50' })
  if (status.value) params.set('status', status.value)
  if (q.value) params.set('q', q.value)
  if (year.value) params.set('year', String(year.value))
  if (department.value) params.set('department', department.value)
  return params.toString()
})

/** Coverage is keyed by year on the audit row; DataTable wants rows. */
const coverageRows = computed(() => Object.entries(baseline.value?.coverage || {})
  .map(([label, counts]) => ({ label, ...counts }))
  .sort((left, right) => right.label.localeCompare(left.label)))

const baselineCounts = computed(() => Object.entries(baseline.value?.counts || {})
  .map(([label, count]) => ({ label: humanize(label), count })))

const queueCounts = computed(() => Object.entries(overview.value?.queue_counts || {})
  .map(([label, count]) => ({ label: humanize(label), key: label, count })))

const pendingGaps = computed(() => Number(overview.value?.queue_counts?.pending ?? 0))

function maintenanceChanged(value: boolean): void {
  const previous = maintenanceActive.value
  maintenanceActive.value = value
  if (value) { clearTimeout(timer); error.value = '' }
  else if (previous) void load()
}

function message(exc: unknown): string {
  const detail = (exc as { payload?: { detail?: string | { message?: string } } })?.payload?.detail
  if (typeof detail === 'string') return detail
  return detail?.message || (exc instanceof Error ? exc.message : String(exc))
}

async function load(): Promise<void> {
  if (disposed || maintenanceActive.value) return
  if (loading.value) { pendingReload = true; return }
  loading.value = true
  clearTimeout(timer)
  try {
    // The overview is fetched on every view: it carries the queue counts each of them
    // reports and the running-job status that decides whether to keep polling.
    const summary = await getMirrorOverview()
    if (disposed) return
    overview.value = summary

    if (view.value === 'gaps') {
      const queue = await getMirrorGaps(query.value)
      gaps.value = queue.items
      total.value = queue.total
    }
    if (view.value === 'audit' && summary.audit) {
      const params = new URLSearchParams({ page: String(findingPage.value), bucket: bucket.value })
      if (!bucket.value) params.delete('bucket')
      const rows = await getMirrorFindings(summary.audit.id, params.toString())
      findings.value = rows.items
      findingTotal.value = rows.total
    }
    if (view.value === 'backfill' && storedJob.value) {
      job.value = await getMirrorJob(storedJob.value)
      const rows = await getMirrorAttempts(storedJob.value, attemptPage.value)
      attempts.value = rows.items
      attemptTotal.value = rows.total
    }
    error.value = ''
  } catch (exc) {
    error.value = message(exc)
  } finally {
    loading.value = false
    if (pendingReload && !disposed) { pendingReload = false; void load(); return }
    if (!disposed && busy.value) timer = setTimeout(() => void load(), 2000)
  }
}

async function act(path: string, body: unknown = {}): Promise<void> {
  submitting.value = true
  error.value = ''
  notice.value = ''
  try {
    const result = await mirrorAction<{ job_id?: string | null; status: string }>(path, body)
    if (result.job_id && path === 'backfill') {
      storedJob.value = result.job_id
      localStorage.setItem('sbpeye-mirror-job', result.job_id)
      attemptPage.value = 1
    }
    notice.value = result.status === 'empty' ? 'No eligible gaps match these controls.'
      : result.status === 'cancelling' ? 'Cancellation requested. Active requests are finishing.'
        : ''
    await load()
  } catch (exc) {
    error.value = message(exc)
  } finally {
    submitting.value = false
  }
}

function startBackfill(): Promise<void> {
  return act('backfill', {
    size: size.value, workers: workers.value, delay: delay.value, max_attempts: maxAttempts.value,
    include_attachments: attachments.value, repeat_until_done: repeat.value, llm_features: selectedFeatures.value,
    statuses: includeFailed.value ? ['pending', 'failed'] : ['pending'],
    years: year.value ? [year.value] : [],
    departments: department.value ? [department.value] : [],
    order: order.value,
  })
}

watch([status, q, year, department], () => { page.value = 1; void load() })
watch(bucket, () => { findingPage.value = 1; void load() })
watch([page, findingPage, attemptPage], () => void load())
watch(view, () => { notice.value = ''; void load() })
onMounted(load)
onUnmounted(() => { disposed = true; clearTimeout(timer) })
</script>

<template>
  <div class="admin-tab-body">
    <IdentityMigrationPanel @maintenance="maintenanceChanged" />

    <template v-if="!maintenanceActive">
      <div class="tab-toolbar">
        <span v-if="overview?.audit" class="muted-text">
          Last audit attempt {{ formatDate(overview.audit.started_at) }}
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

      <Message v-if="error" severity="error" :closable="false">
        {{ error }}
        <Button label="Retry" text size="small" @click="load" />
      </Message>
      <Message v-if="notice" severity="info" :closable="false">{{ notice }}</Message>

      <div v-if="loading && !overview" class="tab-loading">
        <ProgressSpinner style="width: 2rem; height: 2rem" />
      </div>

      <!-- ---- Listing audit ------------------------------------------------------ -->
      <template v-else-if="view === 'audit'">
        <Card class="glass-panel">
          <template #title>Live listing coverage</template>
          <template #content>
            <p v-if="!baseline" class="field-hint">
              No complete assessment yet. Run a full audit to establish a baseline.
            </p>
            <template v-else>
              <p class="headline">
                {{ formatCount(baseline.distinct_total) }}
                <span class="headline-unit">distinct identities</span>
              </p>
              <dl class="summary-list">
                <dt>Last complete audit</dt>
                <dd>{{ formatDate(baseline.started_at) }}</dd>
                <dt>Listing entries</dt>
                <dd>{{ formatCount(baseline.raw_total) }}</dd>
                <template v-for="entry in baselineCounts" :key="entry.label">
                  <dt>{{ entry.label }}</dt>
                  <dd>{{ formatCount(entry.count) }}</dd>
                </template>
              </dl>
              <p class="field-hint">
                Coverage records circular presence only. Search readiness and attachments are
                reported separately, under Documents. Duplicate listings are normal;
                ambiguous identities need review.
              </p>
            </template>

            <p v-if="overview?.audit" class="attempt-line">
              Latest attempt: <AdminStatusChip :status="overview.audit.status" />
              <span v-if="overview.audit.error_code" class="muted-text">{{ overview.audit.error_code }}</span>
              <span v-if="overview.audit.error" class="row-error">{{ overview.audit.error }}</span>
            </p>

            <Message
              v-if="overview?.audit && ['partial', 'failed'].includes(overview.audit.status)"
              severity="warn"
              :closable="false"
            >
              Observed missing listings were added to the queue and can be backfilled. This
              incomplete attempt did not reconcile existing gaps: observed gaps can be
              understated and unlisted local rows inflated.
            </Message>

            <Accordion v-model:value="openDetail" multiple class="detail-accordion">
              <AccordionPanel v-if="coverageRows.length" value="coverage">
                <AccordionHeader>
                  Coverage by year
                  <span class="panel-count">{{ formatCount(coverageRows.length) }} years</span>
                </AccordionHeader>
                <AccordionContent>
                  <DataTable :value="coverageRows" size="small" class="facet-table">
                    <Column field="label" header="Year" style="width: 6rem" />
                    <Column header="Matched"><template #body="{ data }">{{ formatCount(data.matched || 0) }}</template></Column>
                    <Column header="URL matches"><template #body="{ data }">{{ formatCount(data.drifted || 0) }}</template></Column>
                    <Column header="Missing"><template #body="{ data }">{{ formatCount(data.missing || 0) }}</template></Column>
                    <Column header="Ambiguous"><template #body="{ data }">{{ formatCount(data.ambiguous || 0) }}</template></Column>
                  </DataTable>
                </AccordionContent>
              </AccordionPanel>

              <AccordionPanel v-if="overview?.audit?.diagnostics?.length" value="diagnostics">
                <AccordionHeader>
                  Page diagnostics
                  <span class="panel-count">{{ formatCount(overview.audit.diagnostics.length) }} pages</span>
                </AccordionHeader>
                <AccordionContent>
                  <ul class="plain-list">
                    <li v-for="item in overview.audit.diagnostics" :key="item.page">
                      Page {{ item.page + 1 }}: {{ item.error || `${item.yield} entries` }}
                    </li>
                  </ul>
                </AccordionContent>
              </AccordionPanel>
            </Accordion>

            <div class="action-dock">
              <span class="dock-summary">
                A full audit re-reads every listing page at SBP
                <small>{{ workers }} workers · {{ delay }}s between requests — set under Backfill</small>
              </span>
              <Button
                label="Run full audit"
                icon="pi pi-sync"
                size="small"
                :disabled="busy"
                @click="act('audit', { workers, delay })"
              />
            </div>
          </template>
        </Card>

        <Card class="glass-panel">
          <template #title>Audit findings</template>
          <template #content>
            <div class="switch-row">
              <div class="segmented-switch" role="group" aria-label="Finding">
                <button
                  type="button"
                  :aria-pressed="bucket === ''"
                  @click="bucket = ''"
                >
                  All
                </button>
                <button
                  v-for="value in BUCKETS"
                  :key="value"
                  type="button"
                  :aria-pressed="bucket === value"
                  @click="bucket = value"
                >
                  {{ humanize(value) }}
                </button>
              </div>
              <span class="muted-text">{{ formatCount(findingTotal) }} findings</span>
            </div>

            <DataTable :value="findings" size="small" class="facet-table" :loading="loading">
              <Column header="Circular" style="min-width: 18rem">
                <template #body="{ data }">
                  <span>{{ data.descriptor.reference || '—' }}</span>
                  <span class="muted-text row-title">{{ data.descriptor.title }}</span>
                </template>
              </Column>
              <Column header="Finding" style="width: 10rem">
                <template #body="{ data }"><AdminStatusChip :status="data.bucket" /></template>
              </Column>
              <Column header="Evidence" style="min-width: 18rem">
                <template #body="{ data }">
                  <details>
                    <summary>{{ formatCount(data.variants.length) }} listing variants</summary>
                    <ul class="plain-list">
                      <li v-for="variant in data.variants" :key="variant.url">
                        <a :href="variant.url" target="_blank" rel="noopener" class="mono">{{ variant.url }}</a>
                      </li>
                    </ul>
                    <pre class="evidence">{{ data.evidence }}</pre>
                  </details>
                </template>
              </Column>
              <template #empty>No findings in this category.</template>
            </DataTable>

            <div class="pager">
              <Button label="Previous" text size="small" :disabled="findingPage <= 1" @click="findingPage--" />
              <span class="muted-text">Page {{ findingPage }} · {{ formatCount(findingTotal) }} findings</span>
              <Button label="Next" text size="small" :disabled="findingPage * 50 >= findingTotal" @click="findingPage++" />
            </div>
          </template>
        </Card>
      </template>

      <!-- ---- Gap queue ---------------------------------------------------------- -->
      <template v-else-if="view === 'gaps'">
        <div class="stat-grid">
          <div v-for="entry in queueCounts" :key="entry.key" class="stat-tile is-static">
            <span class="stat-value" :class="entry.key === 'pending' && entry.count ? 'tone-warn' : ''">
              {{ formatCount(entry.count) }}
            </span>
            <span class="stat-label">{{ entry.label }}</span>
          </div>
        </div>

        <Card class="glass-panel">
          <template #title>Current gap queue</template>
          <template #content>
            <p class="field-hint">
              These counts are live and change as jobs finish. The listing audit is a
              historical snapshot and does not move with them.
            </p>

            <div class="workbench">
              <aside class="filter-rail">
                <h3 class="section-label">Filters</h3>
                <div>
                  <label for="gap-status">Status</label>
                  <Select
                    id="gap-status"
                    v-model="status"
                    :options="[{ label: 'All', value: '' }, ...QUEUE_STATUSES.map((value) => ({ label: humanize(value), value }))]"
                    option-label="label"
                    option-value="value"
                    size="small"
                  />
                </div>
                <div>
                  <label for="gap-search">Search</label>
                  <InputText id="gap-search" v-model="q" maxlength="200" size="small" />
                </div>
                <div>
                  <label for="gap-year">Year</label>
                  <InputNumber id="gap-year" v-model="year" :min="1000" :max="9999" :use-grouping="false" size="small" />
                </div>
                <div>
                  <label for="gap-department">Department</label>
                  <InputText id="gap-department" v-model="department" maxlength="100" size="small" />
                </div>
                <div>
                  <label for="gap-reason">Reason for skipping</label>
                  <InputText id="gap-reason" v-model="skipReason" maxlength="1000" size="small" />
                </div>
                <a :href="`/api/admin/mirror/gaps.csv?${query}`" class="export-link">Export filtered queue as CSV</a>
              </aside>

              <div class="queue-main">
                <DataTable :value="gaps" size="small" class="facet-table" :loading="loading">
                  <Column header="Circular" style="min-width: 18rem">
                    <template #body="{ data }">
                      <span>{{ data.descriptor.reference || '—' }}</span>
                      <span class="muted-text row-title">{{ data.descriptor.title }}</span>
                    </template>
                  </Column>
                  <Column header="Status" style="width: 12rem">
                    <template #body="{ data }">
                      <AdminStatusChip :status="data.status" />
                      <span v-if="!data.eligible" class="muted-text row-title">
                        Held: {{ data.eligibility_reason }}
                      </span>
                    </template>
                  </Column>
                  <Column header="Attempts" style="min-width: 12rem">
                    <template #body="{ data }">
                      {{ formatCount(data.attempts) }}
                      <span v-if="data.last_error" class="row-error" :title="data.last_error">{{ data.last_error }}</span>
                    </template>
                  </Column>
                  <Column header="" style="width: 12rem">
                    <template #body="{ data }">
                      <div class="row-actions">
                        <Button
                          v-if="['pending', 'failed'].includes(data.status)"
                          label="Skip"
                          text
                          size="small"
                          :disabled="busy || !skipReason.trim()"
                          @click="act(`gaps/${data.id}/skip`, { reason: skipReason })"
                        />
                        <Button
                          v-if="['failed', 'skipped'].includes(data.status)"
                          label="Requeue"
                          text
                          size="small"
                          :disabled="busy"
                          @click="act(`gaps/${data.id}/requeue`)"
                        />
                      </div>
                    </template>
                  </Column>
                  <template #empty>No gaps match these filters.</template>
                </DataTable>

                <div class="pager">
                  <Button label="Previous" text size="small" :disabled="page <= 1" @click="page--" />
                  <span class="muted-text">Page {{ page }} · {{ formatCount(total) }} gaps</span>
                  <Button label="Next" text size="small" :disabled="page * 50 >= total" @click="page++" />
                </div>
              </div>
            </div>
          </template>
        </Card>
      </template>

      <!-- ---- Backfill ----------------------------------------------------------- -->
      <template v-else>
        <Card class="glass-panel">
          <template #title>Backfill controls</template>
          <template #content>
            <p class="field-hint">
              Selects pending gaps by default. Year and department come from the gap queue's
              filters; the text and status filters there affect that table only.
            </p>

            <div class="two-column">
              <div>
                <h3 class="section-label">Rate and scope</h3>
                <div class="control-grid">
                  <div>
                    <label for="bf-size">Batch size</label>
                    <InputNumber id="bf-size" v-model="size" :min="1" :max="500" size="small" />
                  </div>
                  <div>
                    <label for="bf-workers">Workers</label>
                    <InputNumber id="bf-workers" v-model="workers" :min="1" :max="8" size="small" />
                  </div>
                  <div>
                    <label for="bf-delay">Request spacing (s)</label>
                    <InputNumber id="bf-delay" v-model="delay" :min="0" :max="10" :max-fraction-digits="2" size="small" />
                  </div>
                  <div>
                    <label for="bf-attempts">Failure limit</label>
                    <InputNumber id="bf-attempts" v-model="maxAttempts" :min="1" :max="20" size="small" />
                  </div>
                  <div>
                    <label for="bf-order">Order</label>
                    <Select
                      id="bf-order"
                      v-model="order"
                      :options="[
                        { label: 'Newest first', value: 'newest_first' },
                        { label: 'Oldest first', value: 'oldest_first' },
                        { label: 'Fewest attempts', value: 'fewest_attempts' },
                      ]"
                      option-label="label"
                      option-value="value"
                      size="small"
                    />
                  </div>
                </div>

                <h3 class="section-label">What each gap gets</h3>
                <div class="chip-column">
                  <label class="inline-check">
                    <Checkbox v-model="attachments" binary size="small" />
                    Fetch attachments
                  </label>
                  <label class="inline-check">
                    <Checkbox v-model="includeFailed" binary size="small" />
                    Include gaps that failed before
                  </label>
                  <label class="inline-check">
                    <Checkbox v-model="repeat" binary size="small" />
                    Keep going until every eligible gap is done
                  </label>
                </div>
                <p class="field-hint">
                  Attachment fetching is off by default. Fetched attachments are indexed
                  automatically; deferred ones can be picked up later from the Documents
                  workbench.
                </p>
              </div>

              <div>
                <h3 class="section-label">Generate AI analyses</h3>
                <div class="chip-column">
                  <label v-for="feature in FEATURES" :key="feature" class="inline-check">
                    <Checkbox v-model="selectedFeatures" :value="feature" size="small" />
                    {{ feature }}
                  </label>
                </div>
                <p class="field-hint">
                  Runs against each circular as it lands. Leaving these unticked keeps the
                  backfill to fetching and indexing, which is faster and cheaper.
                </p>
              </div>
            </div>

            <div class="action-dock">
              <span class="dock-summary">
                <b>{{ formatCount(pendingGaps) }}</b> gaps pending
                <small>
                  {{ size }} per batch · {{ workers }} workers · {{ delay }}s spacing
                  <template v-if="attachments"> · with attachments</template>
                  <template v-if="selectedFeatures.length"> · {{ selectedFeatures.join(', ') }}</template>
                </small>
              </span>
              <Button
                label="Start backfill"
                icon="pi pi-play"
                size="small"
                :disabled="busy"
                :loading="submitting"
                @click="startBackfill"
              />
            </div>
          </template>
        </Card>

        <Card v-if="job" class="glass-panel">
          <template #title>Backfill progress</template>
          <template #content>
            <dl class="kv-list">
              <dt>Status</dt>
              <dd><AdminStatusChip :status="job.status" /></dd>
              <template v-for="(value, key) in job.progress" :key="key">
                <dt>{{ humanize(String(key)) }}</dt>
                <dd>{{ typeof value === 'number' ? Math.round(value) : value }}</dd>
              </template>
              <template v-if="job.error">
                <dt>Error</dt>
                <dd>{{ job.error }}</dd>
              </template>
            </dl>

            <Message v-if="job.error_count" severity="warn" :closable="false">
              {{ formatCount(job.error_count) }} completed items hit a required-stage error.
              The circulars themselves are stored and resolved; index repair uses the stored
              text and can be run from the Documents workbench.
            </Message>

            <p class="field-hint">
              <RouterLink :to="`/admin/documents/workbench?scope=attachments&finding=actionable&source_job_id=${job.job_id}`">
                Audit this batch and fetch its missing attachments
              </RouterLink>
            </p>

            <h3 class="section-label">Item outcomes</h3>
            <DataTable :value="attempts" size="small" class="facet-table">
              <Column header="Circular" style="min-width: 16rem">
                <template #body="{ data }">{{ data.descriptor.descriptor?.reference || '—' }}</template>
              </Column>
              <Column header="Outcome" style="width: 10rem">
                <template #body="{ data }"><AdminStatusChip :status="data.outcome" /></template>
              </Column>
              <Column header="Stages" style="min-width: 16rem">
                <template #body="{ data }">
                  <span class="mono muted-text">{{ Object.entries(data.stages || {}).map(([name, state]) => `${name}: ${state}`).join(' · ') }}</span>
                  <span v-if="data.error" class="row-error" :title="data.error">{{ data.error }}</span>
                </template>
              </Column>
              <template #empty>No attempts recorded for this job yet.</template>
            </DataTable>

            <div class="pager">
              <Button label="Previous" text size="small" :disabled="attemptPage <= 1" @click="attemptPage--" />
              <span class="muted-text">Page {{ attemptPage }} · {{ formatCount(attemptTotal) }} attempts</span>
              <Button label="Next" text size="small" :disabled="attemptPage * 50 >= attemptTotal" @click="attemptPage++" />
            </div>

            <div v-if="active(job.status)" class="action-dock">
              <span class="dock-summary">
                This job is still running
                <small>cancellation lets active requests finish rather than dropping them</small>
              </span>
              <Button
                label="Cancel after active requests finish"
                severity="secondary"
                outlined
                size="small"
                :disabled="submitting || Boolean(job.progress.cancel_requested)"
                @click="act(`backfill/${job.job_id}/cancel`)"
              />
            </div>
          </template>
        </Card>

        <Message v-else severity="info" :closable="false">
          No backfill has been started from this browser. Starting one here records it so
          progress survives a reload.
        </Message>
      </template>
    </template>
  </div>
</template>

<style scoped>
.queue-main {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  min-width: 0;
}

.control-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(8rem, 1fr));
  gap: 0.75rem;
}

.control-grid label {
  display: block;
  font-size: var(--sbp-fs-meta);
  color: var(--sbp-muted);
  margin-bottom: 0.2rem;
}

.control-grid :deep(.p-inputnumber),
.control-grid :deep(.p-select) {
  width: 100%;
}

.inline-check {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: var(--sbp-fs-sm);
  color: var(--sbp-text);
}

.row-title {
  display: block;
  font-size: var(--sbp-fs-meta);
}

.attempt-line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.4rem;
  margin: 0.75rem 0 0;
  font-size: var(--sbp-fs-sm);
}

.pager {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem;
}

.plain-list {
  margin: 0.35rem 0 0;
  padding-left: 1.1rem;
  font-size: var(--sbp-fs-sm);
}

.plain-list a {
  overflow-wrap: anywhere;
}

.evidence {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: var(--sbp-fs-meta);
  margin: 0.4rem 0 0;
}

.export-link {
  font-size: var(--sbp-fs-meta);
}

.row-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.3rem;
}
</style>
