<script setup lang="ts">
/**
 * One workbench over the corpus: audit what is incomplete, then process an exact batch.
 *
 * This was five routes rendering this same file — Overview, Documents, AI analysis,
 * Search index and Jobs — differing only in which paragraphs showed and which value the
 * Action dropdown started on. Two of those (Overview, Jobs) were never workbenches and
 * now have their own tabs; the other three were the same screen wearing three names, and
 * an operator who changed the Action dropdown was silently doing index work on a tab
 * labelled "AI analysis".
 *
 * So: one route, and `scope` in the query string. It replaces the Action dropdown
 * outright — the thing you are auditing and the thing you are about to run are the same
 * choice, and having them as two independent controls is what let them disagree. Scope
 * also decides the columns, so the AI scope no longer renders vector state and the index
 * scope no longer renders six feature rows nobody is looking at.
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Checkbox from 'primevue/checkbox'
import Column from 'primevue/column'
import DataTable from 'primevue/datatable'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Select from 'primevue/select'

import AdminStatusChip from '@/components/AdminStatusChip.vue'
import { requestJson } from '@/lib/api'
import { formatDate } from './adminFormat'

type Scope = 'attachments' | 'body' | 'ai' | 'index'

interface DocumentRow {
  id: string; reference: string; title: string; department: string; year: number | null;
  body: string; attachments: string; files: { id: string; filename: string; state: string; error?: string }[];
  ai: Record<string, string>; keyword: string; vectors: { id: string; label: string; state: string }[]; index_blocked: boolean;
}

interface Job {
  job_id: string; status: string; started_at: string; error?: string; selection: (string | { id: string })[];
  parameters: { action?: Scope; operation: string; features?: string[] };
  progress: { items?: { id: string; feature?: string; status: string; error?: string }[]; cancel_requested?: boolean };
}

interface Tile {
  key: string
  value: number
  label: string
  sub?: string
  tone?: 'ok' | 'warn' | 'error'
  pressed?: boolean
  /** Absent when the figure has no filter of its own — it reports, it does not narrow. */
  apply?: () => void
}

const SCOPES: Array<{ value: Scope; label: string; verb: string; note: string }> = [
  {
    value: 'attachments',
    label: 'Attachments',
    verb: 'Fetch and index missing attachments',
    note: 'Attachment discovery must run before “no attachments” is known. Categories overlap; unsupported files and unreadable scans stay visible as limitations.',
  },
  {
    value: 'body',
    label: 'Body text',
    verb: 'Recover missing circular bodies',
    note: 'Body text is recovered from the circular page itself. A circular with no body can be neither indexed nor analysed.',
  },
  {
    value: 'ai',
    label: 'AI analysis',
    verb: 'Generate missing AI analyses',
    note: 'Completion records generation, not legal accuracy or freshness after source changes. Consolidation is listed once per existing chain, on its base circular.',
  },
  {
    value: 'index',
    label: 'Search index',
    verb: 'Repair missing or stale search entries',
    note: 'Keyword and vector entries are measured separately. Targeted vector repair is blocked while the embedding configuration differs from the stored index.',
  },
]

const FEATURE_NAMES = ['summary', 'tags', 'checklist', 'relationships', 'entities', 'consolidation']

const route = useRoute()
const router = useRouter()

const rows = ref<DocumentRow[]>([])
const jobs = ref<Job[]>([])
const selectedRows = ref<DocumentRow[]>([])
const features = ref<string[]>([])
const sourceJob = ref(String(route.query.source_job_id || ''))
const search = ref('')
const issue = ref('')
const department = ref('')
const year = ref('')
const size = ref(50)
const all = ref(false)
const loading = ref(false)
const error = ref('')
const measured = ref('')
const verified = ref(false)
const preview = ref(false)
const startedJob = ref<Job | null>(null)

let timer: ReturnType<typeof setTimeout> | undefined
let disposed = false

const scope = computed<Scope>(() => {
  const requested = String(route.query.scope || '')
  return SCOPES.some((entry) => entry.value === requested) ? (requested as Scope) : 'attachments'
})
const scopeMeta = computed(() => SCOPES.find((entry) => entry.value === scope.value) ?? SCOPES[0])

function setScope(next: Scope): void {
  if (next === scope.value) return
  void router.replace({ path: route.path, query: { ...route.query, scope: next } })
}

const active = (job: Job) => ['queued', 'running'].includes(job.status)
const busy = computed(() => loading.value || jobs.value.some(active))

/** Whether this row still has work to do under `operation`. Mirrors the server's own skip. */
function eligible(row: DocumentRow, operation: Scope = scope.value): boolean {
  if (operation === 'body') return row.body === 'missing'
  if (operation === 'attachments') return ['unscanned', 'needs_files'].includes(row.attachments)
  if (operation === 'ai') return features.value.some((feature) => ['missing', 'failed'].includes(row.ai[feature] || ''))
  return !row.index_blocked
    && (['missing', 'stale'].includes(row.keyword) || row.vectors.some((vector) => ['missing', 'stale'].includes(vector.state)))
}

/* Everything the text/department/year filters admit — the denominator the tiles count
   against, so a tile and the table below it never disagree about which rows are in play. */
const scoped = computed(() => rows.value.filter((row) =>
  (!search.value || `${row.reference} ${row.title}`.toLowerCase().includes(search.value.toLowerCase()))
  && (!department.value || row.department === department.value)
  && (!year.value || String(row.year) === year.value)))

const filtered = computed(() => scoped.value.filter((row) =>
  !issue.value || (issue.value === 'actionable' ? eligible(row)
    : issue.value === 'body' ? row.body === 'missing'
      : ['missing_file', 'extraction_failed'].includes(issue.value) ? row.files.some((file) => file.state === issue.value)
        : row.attachments === issue.value)))

const eligibleMatches = computed(() => filtered.value.filter((row) => eligible(row)))
const departments = computed(() => [...new Set(rows.value.map((row) => row.department).filter(Boolean))].sort())
const years = computed(() => [...new Set(rows.value.map((row) => row.year).filter(Boolean))].sort().reverse())

const targets = computed(() => {
  const ids = new Set(selectedRows.value.map((row) => row.id))
  const candidates = eligibleMatches.value.filter((row) => !ids.size || ids.has(row.id))
  return all.value ? candidates : candidates.slice(0, size.value)
})

function focusFinding(finding: string): void {
  issue.value = issue.value === finding ? '' : finding
}

const attachmentCounts = computed(() => ({
  actionable: scoped.value.filter((row) => eligible(row, 'attachments')).length,
  unscanned: scoped.value.filter((row) => row.attachments === 'unscanned').length,
  missingFiles: scoped.value.reduce((total, row) => total + row.files.filter((file) => file.state === 'missing_file').length, 0),
  missingCirculars: scoped.value.filter((row) => row.files.some((file) => file.state === 'missing_file')).length,
  extractionFailures: scoped.value.reduce((total, row) => total + row.files.filter((file) => file.state === 'extraction_failed').length, 0),
  extractionCirculars: scoped.value.filter((row) => row.files.some((file) => file.state === 'extraction_failed')).length,
}))

const tiles = computed<Tile[]>(() => {
  if (scope.value === 'attachments') {
    const counts = attachmentCounts.value
    return [
      { key: 'actionable', value: counts.actionable, label: 'need attachment work', tone: counts.actionable ? 'warn' : 'ok', pressed: issue.value === 'actionable', apply: () => focusFinding('actionable') },
      { key: 'unscanned', value: counts.unscanned, label: 'never scanned', sub: 'attachment count unknown', pressed: issue.value === 'unscanned', apply: () => focusFinding('unscanned') },
      { key: 'missing_file', value: counts.missingFiles, label: 'files missing', sub: `across ${counts.missingCirculars.toLocaleString()} circulars`, tone: counts.missingFiles ? 'error' : undefined, pressed: issue.value === 'missing_file', apply: () => focusFinding('missing_file') },
      { key: 'extraction_failed', value: counts.extractionFailures, label: 'files need text extraction', sub: `across ${counts.extractionCirculars.toLocaleString()} circulars`, tone: counts.extractionFailures ? 'warn' : undefined, pressed: issue.value === 'extraction_failed', apply: () => focusFinding('extraction_failed') },
    ]
  }
  if (scope.value === 'body') {
    const missing = scoped.value.filter((row) => row.body === 'missing').length
    return [
      { key: 'missing', value: missing, label: 'missing body text', tone: missing ? 'error' : 'ok', pressed: issue.value === 'body', apply: () => focusFinding('body') },
      { key: 'stored', value: scoped.value.length - missing, label: 'body text stored' },
    ]
  }
  if (scope.value === 'ai') {
    return FEATURE_NAMES.map((feature) => {
      const count = scoped.value.filter((row) => ['missing', 'failed'].includes(row.ai[feature] || '')).length
      return {
        key: feature,
        value: count,
        label: `missing ${feature}`,
        tone: count ? 'error' : 'ok',
        // Picking a feature is picking what the batch will generate, so the tile sets
        // both — otherwise "3,647 missing summary" filtered to rows the batch skips.
        pressed: features.value.length === 1 && features.value[0] === feature,
        apply: () => { features.value = [feature]; issue.value = 'actionable' },
      } satisfies Tile
    })
  }
  const blocked = scoped.value.filter((row) => row.index_blocked).length
  const actionable = scoped.value.filter((row) => eligible(row, 'index')).length
  return [
    { key: 'actionable', value: actionable, label: 'need index repair', tone: actionable ? 'warn' : 'ok', pressed: issue.value === 'actionable', apply: () => focusFinding('actionable') },
    { key: 'keyword', value: scoped.value.filter((row) => ['missing', 'stale'].includes(row.keyword)).length, label: 'keyword entries missing or stale' },
    { key: 'vectors', value: scoped.value.filter((row) => row.vectors.some((vector) => ['missing', 'stale'].includes(vector.state))).length, label: 'vector sources missing or stale' },
    { key: 'blocked', value: blocked, label: 'blocked by embedding drift', tone: blocked ? 'error' : undefined },
  ]
})

const findingOptions = computed(() => {
  const common = [
    { label: 'All matching circulars', value: '' },
    { label: 'Eligible for this scope', value: 'actionable' },
  ]
  if (scope.value === 'attachments') {
    return [
      ...common,
      { label: 'Attachments never scanned', value: 'unscanned' },
      { label: 'Known attachment files missing', value: 'missing_file' },
      { label: 'Files need text extraction', value: 'extraction_failed' },
      { label: 'Download or extraction problems', value: 'needs_files' },
      { label: 'Unsupported or unreadable files', value: 'limited' },
    ]
  }
  if (scope.value === 'body') return [...common, { label: 'Missing body text', value: 'body' }]
  return common
})

const sourceJobOptions = computed(() => [
  { label: 'All stored circulars', value: '' },
  ...jobs.value.map((job) => ({
    label: `${formatDate(job.started_at)} · ${job.parameters.action || 'backfill'} · ${job.job_id.slice(0, 8)}`,
    value: job.job_id,
  })),
])

async function load(verify = false): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const params = new URLSearchParams({ verify: String(verify) })
    if (sourceJob.value) params.set('source_job_id', sourceJob.value)
    const [assessment, history] = await Promise.all([
      requestJson<{ items: DocumentRow[]; generated_at: string; verified: boolean }>(`/admin/maintenance/documents?${params}`),
      requestJson<Job[]>('/admin/maintenance/jobs'),
    ])
    if (disposed) return
    rows.value = assessment.items
    measured.value = assessment.generated_at
    verified.value = assessment.verified
    jobs.value = history
    if (startedJob.value) {
      startedJob.value = history.find((job) => job.job_id === startedJob.value?.job_id) || startedJob.value
    }
  } catch (exc) {
    error.value = String(exc)
  } finally {
    loading.value = false
    schedule()
  }
}

function schedule(): void {
  clearTimeout(timer)
  if (!disposed && jobs.value.some(active)) timer = setTimeout(() => void load(false), 3000)
}

async function start(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const result = await requestJson<{ job_id: string }>('/circulars/maintenance/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ids: targets.value.map((row) => row.id),
        action: scope.value,
        features: features.value,
        size: size.value,
        source_job_id: sourceJob.value || null,
      }),
    })
    startedJob.value = await requestJson<Job>(`/admin/maintenance/jobs/${result.job_id}`)
    preview.value = false
    selectedRows.value = []
    await load()
  } catch (exc) {
    error.value = String(exc)
    loading.value = false
  }
}

const featureTaskCount = computed(() => targets.value.reduce(
  (total, row) => total + features.value.filter((feature) => ['missing', 'failed'].includes(row.ai[feature] || '')).length,
  0,
))

watch([search, issue, department, year, features, selectedRows, size, all], () => { preview.value = false }, { deep: true })
watch(sourceJob, () => { selectedRows.value = []; void load(false) })

watch(scope, () => {
  // Every scope has its own vocabulary of findings; carrying `unscanned` into the AI
  // scope would silently filter to rows that scope cannot act on.
  if (!findingOptions.value.some((option) => option.value === issue.value)) issue.value = ''
  if (scope.value === 'ai' && !features.value.length) features.value = [...FEATURE_NAMES]
  selectedRows.value = []
  preview.value = false
}, { immediate: true })

/*
 * `?finding=` lets Overview hand off a specific finding rather than a scope.
 * "98 files failed extraction" used to open a tab where you then had to rediscover
 * which of seven dropdown entries meant that, so the link arrived one step short.
 */
watch(() => route.query.finding, (requested) => {
  const value = String(requested || '')
  if (value && findingOptions.value.some((option) => option.value === value)) issue.value = value
}, { immediate: true })

onMounted(() => void load())
onUnmounted(() => { disposed = true; clearTimeout(timer) })
</script>

<template>
  <div class="admin-tab-body">
    <div class="tab-toolbar">
      <span v-if="measured" class="muted-text">
        {{ verified ? 'Index contents checked' : 'Recorded index status' }} · {{ formatDate(measured) }}
      </span>
      <Button
        label="Verify search indexes"
        icon="pi pi-search"
        severity="secondary"
        outlined
        size="small"
        :disabled="busy"
        @click="load(true)"
      />
      <Button
        label="Refresh"
        icon="pi pi-refresh"
        severity="secondary"
        outlined
        size="small"
        :loading="loading"
        @click="load(false)"
      />
    </div>

    <Message v-if="error" severity="error" :closable="false">{{ error }}</Message>

    <Message v-if="startedJob" severity="info" :closable="false">
      Batch <span class="mono">{{ startedJob.job_id.slice(0, 8) }}</span> is
      {{ startedJob.status }}. It continues when you close this page —
      <RouterLink to="/admin/jobs">follow it under Jobs</RouterLink>.
    </Message>

    <Message v-if="rows.some((row) => row.index_blocked)" severity="warn" :closable="false">
      Embedding configuration differs from the stored index, so targeted vector repairs are
      blocked. The configuration and the full rebuild procedure are under
      <RouterLink to="/admin/system/search-index">System → Search index</RouterLink>.
    </Message>

    <div class="switch-row">
      <div class="segmented-switch" role="group" aria-label="Maintenance scope">
        <button
          v-for="entry in SCOPES"
          :key="entry.value"
          type="button"
          :aria-pressed="scope === entry.value"
          @click="setScope(entry.value)"
        >
          {{ entry.label }}
        </button>
      </div>
      <span class="muted-text">{{ scopeMeta.verb }}</span>
    </div>

    <div class="stat-grid">
      <template v-for="tile in tiles" :key="tile.key">
        <button
          v-if="tile.apply"
          type="button"
          class="stat-tile"
          :aria-pressed="Boolean(tile.pressed)"
          @click="tile.apply?.()"
        >
          <span class="stat-value" :class="tile.tone ? `tone-${tile.tone}` : ''">{{ tile.value.toLocaleString() }}</span>
          <span class="stat-label">{{ tile.label }}</span>
          <span v-if="tile.sub" class="stat-sub">{{ tile.sub }}</span>
        </button>
        <div v-else class="stat-tile is-static">
          <span class="stat-value" :class="tile.tone ? `tone-${tile.tone}` : ''">{{ tile.value.toLocaleString() }}</span>
          <span class="stat-label">{{ tile.label }}</span>
          <span v-if="tile.sub" class="stat-sub">{{ tile.sub }}</span>
        </div>
      </template>
    </div>

    <p class="field-hint">{{ scopeMeta.note }}</p>

    <div class="workbench">
      <aside class="filter-rail">
        <h3 class="section-label">Filters</h3>
        <div>
          <label for="wb-search">Reference or title</label>
          <InputText id="wb-search" v-model="search" size="small" maxlength="200" />
        </div>
        <div>
          <label for="wb-department">Department</label>
          <Select
            id="wb-department"
            v-model="department"
            :options="[{ label: 'All', value: '' }, ...departments.map((name) => ({ label: name, value: name }))]"
            option-label="label"
            option-value="value"
            size="small"
          />
        </div>
        <div>
          <label for="wb-year">Year</label>
          <Select
            id="wb-year"
            v-model="year"
            :options="[{ label: 'All', value: '' }, ...years.map((value) => ({ label: String(value), value: String(value) }))]"
            option-label="label"
            option-value="value"
            size="small"
          />
        </div>
        <div>
          <label for="wb-finding">Finding</label>
          <Select
            id="wb-finding"
            v-model="issue"
            :options="findingOptions"
            option-label="label"
            option-value="value"
            size="small"
          />
        </div>
        <div>
          <label for="wb-source">Source batch</label>
          <Select
            id="wb-source"
            v-model="sourceJob"
            :options="sourceJobOptions"
            option-label="label"
            option-value="value"
            size="small"
          />
        </div>

        <h3 class="section-label">Batch</h3>
        <div v-if="scope === 'ai'" class="chip-column">
          <label v-for="feature in FEATURE_NAMES" :key="feature" class="inline-check">
            <Checkbox v-model="features" :value="feature" size="small" />
            {{ feature }}
          </label>
        </div>
        <div>
          <label for="wb-size">Batch size</label>
          <InputNumber id="wb-size" v-model="size" :min="1" :max="500" size="small" :disabled="all" />
        </div>
        <label class="inline-check">
          <Checkbox v-model="all" binary size="small" />
          Process every eligible match, in batches
        </label>

        <Button label="Reset filters" text size="small" @click="search = ''; department = ''; year = ''; issue = ''" />
      </aside>

      <div class="workbench-main">
        <DataTable
          v-model:selection="selectedRows"
          :value="filtered"
          data-key="id"
          size="small"
          class="facet-table"
          paginator
          :rows="50"
          :loading="loading"
          scrollable
        >
          <Column selection-mode="multiple" header-style="width: 3rem" />
          <Column header="Circular" style="min-width: 16rem">
            <template #body="{ data }">
              <RouterLink :to="`/circulars/${data.id}`">{{ data.reference }}</RouterLink>
              <span class="muted-text row-title">{{ data.title }}</span>
              <span class="muted-text row-meta">{{ data.department }} · {{ data.year }}</span>
            </template>
          </Column>

          <template v-if="scope === 'attachments'">
            <Column header="Attachments" style="width: 10rem">
              <template #body="{ data }">
                <AdminStatusChip :status="data.attachments" />
              </template>
            </Column>
            <Column header="Files" style="min-width: 14rem">
              <template #body="{ data }">
                <span v-if="!data.files.length" class="muted-text">none recorded</span>
                <div v-for="file in data.files" :key="file.id" class="file-row">
                  <span class="mono">{{ file.filename }}</span>
                  <AdminStatusChip :status="file.state" />
                  <span v-if="file.error" class="row-error" :title="file.error">{{ file.error }}</span>
                </div>
              </template>
            </Column>
          </template>

          <template v-else-if="scope === 'body'">
            <Column header="Body text" style="width: 10rem">
              <template #body="{ data }">
                <AdminStatusChip :status="data.body" />
              </template>
            </Column>
            <Column header="Attachments" style="width: 10rem">
              <template #body="{ data }">
                <AdminStatusChip :status="data.attachments" />
              </template>
            </Column>
          </template>

          <template v-else-if="scope === 'ai'">
            <Column header="Analysis" style="min-width: 20rem">
              <template #body="{ data }">
                <div class="feature-grid">
                  <span v-for="feature in FEATURE_NAMES" :key="feature" class="feature-state">
                    <span class="muted-text">{{ feature }}</span>
                    <AdminStatusChip :status="data.ai[feature] || 'unknown'" />
                  </span>
                </div>
              </template>
            </Column>
          </template>

          <template v-else>
            <Column header="Keyword" style="width: 9rem">
              <template #body="{ data }">
                <AdminStatusChip :status="data.keyword" />
              </template>
            </Column>
            <Column header="Vector sources" style="min-width: 14rem">
              <template #body="{ data }">
                <span v-if="!data.vectors.length" class="muted-text">none recorded</span>
                <div v-for="vector in data.vectors" :key="vector.id" class="file-row">
                  <span>{{ vector.label }}</span>
                  <AdminStatusChip :status="vector.state" />
                </div>
              </template>
            </Column>
          </template>

          <template #empty>No documents match these filters.</template>
        </DataTable>

        <Card v-if="preview" class="glass-panel">
          <template #title>Batch preview</template>
          <template #content>
            <p>
              {{ targets.length.toLocaleString() }} documents will receive
              {{ scopeMeta.label.toLowerCase() }} maintenance<template v-if="scope === 'ai'">:
                {{ featureTaskCount.toLocaleString() }} feature tasks for {{ features.join(', ') || 'nothing — pick a feature' }}.
                Relationships run before consolidation across the batch</template>.
            </p>
            <ul class="preview-list">
              <li v-for="row in targets" :key="row.id">
                <span class="mono">{{ row.reference }}</span> — {{ row.title }}
              </li>
            </ul>
          </template>
        </Card>

        <div class="action-dock">
          <span class="dock-summary">
            <b>{{ eligibleMatches.length.toLocaleString() }}</b> eligible ·
            <b>{{ targets.length.toLocaleString() }}</b> in this batch ·
            {{ selectedRows.length.toLocaleString() }} selected of {{ filtered.length.toLocaleString() }} shown
            <small>{{ scopeMeta.verb }} · completed work is skipped · a batch continues when you close this page</small>
          </span>
          <Button
            label="Clear selection"
            text
            size="small"
            :disabled="!selectedRows.length"
            @click="selectedRows = []"
          />
          <Button
            label="Preview batch"
            severity="secondary"
            outlined
            size="small"
            :disabled="busy || !targets.length"
            @click="preview = true"
          />
          <Button
            label="Start batch"
            icon="pi pi-play"
            size="small"
            :disabled="busy || !preview || !targets.length"
            @click="start"
          />
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.workbench-main {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  min-width: 0;
}

.row-title,
.row-meta {
  display: block;
  font-size: var(--sbp-fs-meta);
}

.file-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.4rem;
  padding: 0.1rem 0;
}

.feature-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
}

.feature-state {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  font-size: var(--sbp-fs-meta);
}

.inline-check {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: var(--sbp-fs-sm);
  color: var(--sbp-text);
}

/* A batch can be every eligible match, so the confirmation list scrolls rather than
   pushing the Start button off the far end of the page. */
.preview-list {
  margin: 0;
  padding-left: 1.1rem;
  max-height: 16rem;
  overflow-y: auto;
  font-size: var(--sbp-fs-sm);
}
</style>
