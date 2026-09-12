<script setup lang="ts">
/**
 * Where the corpus stands, as one read.
 *
 * The old overview was four link-tiles and a paragraph telling you which other tab held
 * the rest. It is the only screen most operators open on a normal day, and it was the
 * thinnest in the console.
 *
 * Two ideas replace it. The pipeline: coverage is already measured independently at each
 * stage — listed, stored, attached, indexed, analysed — and a stack of bars says in one
 * glance which stage is the gap, where four separate counts said nothing about each
 * other. And the attention list: every row names a finding, carries its severity in the
 * stripe rather than only in the digits, and offers exactly one verb, which opens the
 * workbench already filtered to that finding.
 *
 * Sources are fetched with `allSettled` rather than `all` on purpose. The mirror
 * overview talks to a table the identity migration locks, and one unavailable source
 * should cost its own card, not the page.
 */
import { computed, onMounted, ref } from 'vue'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Message from 'primevue/message'
import ProgressSpinner from 'primevue/progressspinner'

import AdminCoverageBar from '@/components/AdminCoverageBar.vue'
import AdminStatusChip from '@/components/AdminStatusChip.vue'
import {
  getAdminCorpusStatus, getMirrorOverview, requestJson,
  type AdminCorpusStatus, type MirrorOverview,
} from '@/lib/api'
import { formatCount, formatDate } from './adminFormat'

interface DocumentRow {
  id: string; body: string; attachments: string
  files: { state: string }[]
  ai: Record<string, string>
  keyword: string
  vectors: { state: string }[]
  index_blocked: boolean
}

interface Job {
  job_id: string; status: string; started_at: string
  parameters: { action?: string; operation: string }
}

interface Attention {
  key: string
  value: number
  headline: string
  detail: string
  tone: 'ok' | 'warn' | 'error'
  action?: { label: string; to: string }
}

const corpus = ref<AdminCorpusStatus | null>(null)
const mirror = ref<MirrorOverview | null>(null)
const rows = ref<DocumentRow[]>([])
const jobs = ref<Job[]>([])
const measured = ref('')
const loading = ref(false)
const error = ref('')
const partial = ref<string[]>([])

const total = computed(() => rows.value.length)
const runningJobs = computed(() => jobs.value.filter((job) => ['queued', 'running'].includes(job.status)))

const withBody = computed(() => rows.value.filter((row) => row.body !== 'missing').length)
/* Indexed means nothing is missing or stale, not that a particular word appears: the
   assessment reports `recorded` for a healthy keyword entry, and `blocked` rows are
   held by embedding drift rather than by anything a repair here would fix. */
const searchIndexed = computed(() => rows.value.filter((row) =>
  !['missing', 'stale'].includes(row.keyword)
  && !row.vectors.some((vector) => ['missing', 'stale'].includes(vector.state))).length)
const attachmentsSettled = computed(() => rows.value.filter((row) => !['unscanned', 'needs_files'].includes(row.attachments)).length)
const analysed = computed(() => rows.value.filter((row) => Object.values(row.ai).every((state) => !['missing', 'failed'].includes(state))).length)

/**
 * The stages a circular passes through, in order, each measured against the same corpus.
 *
 * Deliberately the same denominator for every row: a stage that covers 400 of its own
 * 500 candidates looks healthy on its own terms and is still a hole in the corpus.
 */
const pipeline = computed(() => [
  { feature: 'listed at SBP', generated: total.value, total: total.value },
  { feature: 'body text stored', generated: withBody.value, total: total.value },
  { feature: 'attachments settled', generated: attachmentsSettled.value, total: total.value },
  { feature: 'search indexed', generated: searchIndexed.value, total: total.value },
  { feature: 'fully analysed', generated: analysed.value, total: total.value },
])

const attention = computed<Attention[]>(() => {
  if (!rows.value.length) return []
  const items: Attention[] = []

  const unanalysed = rows.value.filter((row) => Object.values(row.ai).some((state) => ['missing', 'failed'].includes(state))).length
  items.push({
    key: 'ai',
    value: unanalysed,
    headline: 'circulars have incomplete AI analysis',
    detail: 'summary, tags, checklist, relationships, entities or consolidation still missing',
    tone: unanalysed ? 'error' : 'ok',
    action: unanalysed ? { label: 'Open in workbench', to: '/admin/documents/workbench?scope=ai&finding=actionable' } : undefined,
  })

  const attachmentWork = rows.value.filter((row) => ['unscanned', 'needs_files'].includes(row.attachments)).length
  items.push({
    key: 'attachments',
    value: attachmentWork,
    headline: 'circulars need attachment work',
    detail: 'never scanned, or a known file failed to download',
    tone: attachmentWork ? 'warn' : 'ok',
    action: attachmentWork ? { label: 'Open in workbench', to: '/admin/documents/workbench?scope=attachments&finding=actionable' } : undefined,
  })

  const extractionFailures = rows.value.reduce((count, row) => count + row.files.filter((file) => file.state === 'extraction_failed').length, 0)
  items.push({
    key: 'extraction',
    value: extractionFailures,
    headline: 'attachment files need text extraction',
    detail: 'downloaded, but no text has been read out of them yet',
    tone: extractionFailures ? 'warn' : 'ok',
    action: extractionFailures ? { label: 'Open in workbench', to: '/admin/documents/workbench?scope=attachments&finding=extraction_failed' } : undefined,
  })

  const missingBody = rows.value.filter((row) => row.body === 'missing').length
  if (missingBody) {
    items.push({
      key: 'body',
      value: missingBody,
      headline: 'circulars have no body text',
      detail: 'these can be neither indexed nor analysed until the body is recovered',
      tone: 'error',
      action: { label: 'Open in workbench', to: '/admin/documents/workbench?scope=body&finding=body' },
    })
  }

  const indexRepair = rows.value.filter((row) => !row.index_blocked
    && (['missing', 'stale'].includes(row.keyword) || row.vectors.some((vector) => ['missing', 'stale'].includes(vector.state)))).length
  items.push({
    key: 'index',
    value: indexRepair,
    headline: 'circulars need search index repair',
    detail: 'keyword or vector entries are missing or stale',
    tone: indexRepair ? 'warn' : 'ok',
    action: indexRepair ? { label: 'Open in workbench', to: '/admin/documents/workbench?scope=index&finding=actionable' } : undefined,
  })

  const pending = Number(mirror.value?.queue_counts?.pending ?? 0)
  if (mirror.value) {
    items.push({
      key: 'gaps',
      value: pending,
      headline: 'circulars listed at SBP but not held here',
      detail: mirror.value.latest_complete_audit
        ? `last complete listing audit ${formatDate(mirror.value.latest_complete_audit.started_at)}`
        : 'no complete listing audit has run yet',
      tone: pending ? 'error' : 'ok',
      action: { label: pending ? 'Open gap queue' : 'View queue', to: '/admin/ingest/gaps' },
    })
  }

  // Worst first. A console opened at a glance should put the thing that needs a decision
  // at the top, and "0 pending gaps" is reassurance, not a task.
  const rank = { error: 0, warn: 1, ok: 2 }
  return items.sort((left, right) => rank[left.tone] - rank[right.tone] || right.value - left.value)
})

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  partial.value = []

  const [assessment, jobHistory, corpusStatus, mirrorOverview] = await Promise.allSettled([
    requestJson<{ items: DocumentRow[]; generated_at: string }>('/admin/maintenance/documents?verify=false'),
    requestJson<Job[]>('/admin/maintenance/jobs'),
    getAdminCorpusStatus(),
    getMirrorOverview(),
  ])

  if (assessment.status === 'fulfilled') {
    rows.value = assessment.value.items
    measured.value = assessment.value.generated_at
  } else {
    error.value = (assessment.reason as Error)?.message || 'Could not read the coverage assessment.'
  }
  if (jobHistory.status === 'fulfilled') jobs.value = jobHistory.value
  else partial.value.push('running jobs')

  if (corpusStatus.status === 'fulfilled') corpus.value = corpusStatus.value
  else partial.value.push('corpus statistics')

  if (mirrorOverview.status === 'fulfilled') mirror.value = mirrorOverview.value
  else partial.value.push('the listing audit')

  loading.value = false
}

onMounted(load)
</script>

<template>
  <div class="admin-tab-body">
    <div class="tab-toolbar">
      <span v-if="measured" class="muted-text">Read {{ formatDate(measured) }}</span>
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
    <Message v-else-if="partial.length" severity="warn" :closable="false">
      Could not read {{ partial.join(' or ') }}. Everything else on this page is current.
    </Message>

    <div v-if="loading && !rows.length" class="tab-loading">
      <ProgressSpinner style="width: 2rem; height: 2rem" />
    </div>

    <template v-else-if="rows.length">
      <Message v-if="runningJobs.length" severity="info" :closable="false">
        {{ runningJobs.length }} job{{ runningJobs.length === 1 ? '' : 's' }} in flight ·
        <RouterLink to="/admin/jobs">follow under Jobs</RouterLink>
      </Message>

      <Card class="glass-panel">
        <template #title>Pipeline coverage</template>
        <template #content>
          <div class="coverage-stack">
            <AdminCoverageBar
              v-for="stage in pipeline"
              :key="stage.feature"
              :feature="stage.feature"
              :generated="stage.generated"
              :total="stage.total"
            />
          </div>
          <p class="field-hint">
            Each stage is measured on its own against all {{ formatCount(total) }} circulars.
            A circular can be searchable without attachments and analysed without being
            consolidated, so these are five independent readings rather than one funnel a
            document falls through.
          </p>
        </template>
      </Card>

      <Card class="glass-panel">
        <template #title>Needs attention</template>
        <template #content>
          <div class="attn-list">
            <div v-for="item in attention" :key="item.key" class="attn" :class="`tone-${item.tone}`">
              <span class="attn-figure">{{ formatCount(item.value) }}</span>
              <span class="attn-body">
                {{ item.headline }}
                <small>{{ item.detail }}</small>
              </span>
              <RouterLink v-if="item.action" :to="item.action.to">
                <Button :label="item.action.label" severity="secondary" outlined size="small" />
              </RouterLink>
            </div>
          </div>
        </template>
      </Card>

      <Card v-if="corpus" class="glass-panel">
        <template #title>Analysis coverage by feature</template>
        <template #content>
          <div class="coverage-stack">
            <AdminCoverageBar
              v-for="entry in corpus.circulars.coverage"
              :key="entry.feature"
              :feature="entry.feature"
              :generated="entry.generated"
              :total="entry.total"
            />
          </div>
          <p class="field-hint">
            Per-feature, across circulars. Department and year breakdowns, the laws corpus
            and relationship counts are under
            <RouterLink to="/admin/documents/corpus">Documents → Corpus statistics</RouterLink>.
          </p>
        </template>
      </Card>

      <div v-if="corpus" class="summary-columns">
        <Card class="glass-panel summary-column">
          <template #title>Circulars</template>
          <template #content>
            <p class="headline">{{ formatCount(corpus.circulars.total) }}</p>
            <dl class="summary-list">
              <dt>Departments</dt>
              <dd>{{ formatCount(corpus.circulars.by_department.length) }}</dd>
              <dt>Latest</dt>
              <dd>
                {{ corpus.circulars.latest?.reference || corpus.circulars.latest?.title || '—' }}
                <span class="muted-text">{{ formatDate(corpus.circulars.latest?.date) }}</span>
              </dd>
            </dl>
          </template>
        </Card>

        <Card class="glass-panel summary-column">
          <template #title>Attachments</template>
          <template #content>
            <p class="headline">{{ formatCount(corpus.attachments.total) }}</p>
            <dl class="summary-list">
              <dt>With text</dt>
              <dd>{{ formatCount(corpus.attachments.with_text) }}</dd>
              <dt>Vectorized</dt>
              <dd>{{ formatCount(corpus.attachments.vectorized) }}</dd>
              <dt>With error</dt>
              <dd>{{ formatCount(corpus.attachments.with_error) }}</dd>
            </dl>
          </template>
        </Card>

        <Card class="glass-panel summary-column">
          <template #title>Laws &amp; regulations</template>
          <template #content>
            <p class="headline">{{ formatCount(corpus.laws.documents) }}</p>
            <dl class="summary-list">
              <dt>Versions</dt>
              <dd>{{ formatCount(corpus.laws.versions) }}</dd>
              <dt>In force</dt>
              <dd>{{ formatCount(corpus.laws.current_versions) }}</dd>
              <dt>Circular-backed</dt>
              <dd>{{ formatCount(corpus.laws.circular_backed) }}</dd>
            </dl>
          </template>
        </Card>

        <Card v-if="mirror" class="glass-panel summary-column">
          <template #title>Listing audit</template>
          <template #content>
            <p class="headline">
              {{ formatCount(mirror.latest_complete_audit?.distinct_total) }}
              <span class="headline-unit">identities</span>
            </p>
            <dl class="summary-list">
              <dt>Last attempt</dt>
              <dd>
                <AdminStatusChip v-if="mirror.audit" :status="mirror.audit.status" />
                <template v-else>—</template>
                <span class="muted-text">{{ formatDate(mirror.audit?.started_at) }}</span>
              </dd>
              <dt>Queue</dt>
              <dd>
                <RouterLink to="/admin/ingest/gaps">
                  {{ formatCount(Number(mirror.queue_counts?.pending ?? 0)) }} pending
                </RouterLink>
              </dd>
            </dl>
          </template>
        </Card>
      </div>
    </template>
  </div>
</template>

<style scoped>
/* The button is the row's one verb, so the link that wraps it must not also look like
   a link — otherwise the row carries two competing affordances for the same action. */
.attn a {
  text-decoration: none;
}
</style>
