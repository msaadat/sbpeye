<script setup lang="ts">
/**
 * What the corpus holds, and how much of it has been analysed.
 *
 * Three readings, in descending order of how often you want them. The summary columns
 * answer "how big is each thing" at a glance; the coverage card answers "how far has
 * analysis got", which needs width for the bars to mean anything; the breakdowns are
 * folded away because a 29-row department table is reference data, not a headline.
 *
 * Read-only: nothing here starts a sync or a generation run — those still belong to the
 * CLI, because on this deployment they largely cannot run at all (see the SBP banner).
 */
import { computed, onMounted, ref } from 'vue'
import Accordion from 'primevue/accordion'
import AccordionContent from 'primevue/accordioncontent'
import AccordionHeader from 'primevue/accordionheader'
import AccordionPanel from 'primevue/accordionpanel'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Column from 'primevue/column'
import DataTable from 'primevue/datatable'
import Message from 'primevue/message'
import ProgressSpinner from 'primevue/progressspinner'

import AdminCoverageBar from '@/components/AdminCoverageBar.vue'
import AdminStatusChip from '@/components/AdminStatusChip.vue'
import {
  getAdminCorpusStatus,
  getAppStatus,
  type AdminCorpusStatus,
  type AppStatus,
} from '@/lib/api'
import { ABSENT, facetRows, formatCount, formatDate, percent } from './adminFormat'

// Which detail panels are open. A real model, not a literal `[]` on the Accordion:
// that binding hands the component a fresh empty array on every render, so a panel
// closes again the instant anything else on the page updates.
const openDetail = ref<string[]>([])

const status = ref<AdminCorpusStatus | null>(null)
const appStatus = ref<AppStatus | null>(null)
const loading = ref(false)
const error = ref('')

const circulars = computed(() => status.value?.circulars)
const attachments = computed(() => status.value?.attachments)
const laws = computed(() => status.value?.laws)

/**
 * Whether SBP is reachable from wherever this instance runs.
 *
 * Reused from `/api/app/status`, which the sidebar already polls, rather than probed
 * again here — one answer, one cache, no chance of the banner and the badge disagreeing.
 * On the Railway deployment this reads `error`, and that is the expected state rather
 * than a fault: the IP is blocked, so corpus updates happen on a maintainer's machine.
 */
const remoteState = computed(() => appStatus.value?.remote_check_status ?? null)

const extractionFacets = computed(() => facetRows(attachments.value?.by_extraction_status))
const lawExtractionFacets = computed(() => facetRows(laws.value?.by_extraction_status))
const circularStatusFacets = computed(() => facetRows(circulars.value?.by_status))
const relationshipFacets = computed(() => facetRows(status.value?.relationships.by_type))

/** Relationship edges naming a circular we do not hold — `resolve-targets` closes these. */
const unresolvedRelationships = computed(() => {
  const rel = status.value?.relationships
  return rel ? rel.total - rel.resolved : 0
})

/**
 * How much of the whole analysis matrix is filled: every feature, over both corpora.
 *
 * Both halves of the fraction are summed before dividing, so a feature with a large
 * denominator counts for more than one with a small one — averaging the five percentages
 * instead would let two analysed law editions offset three thousand unanalysed circulars.
 */
const overallCoverage = computed(() => {
  const rows = [...(circulars.value?.coverage || []), ...(laws.value?.coverage || [])]
  const generated = rows.reduce((sum, row) => sum + row.generated, 0)
  const total = rows.reduce((sum, row) => sum + row.total, 0)
  return { generated, total, percent: percent(generated, total) }
})

const circularYears = computed(() => {
  const range = circulars.value
  if (!range?.earliest_date || !range?.latest_date) return ABSENT
  return `${new Date(range.earliest_date).getFullYear()}–${new Date(range.latest_date).getFullYear()}`
})

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    // The corpus numbers are the page; SBP reachability is context for them. A failure
    // to reach the latter must not blank the former, hence `allSettled`.
    const [corpus, app] = await Promise.allSettled([getAdminCorpusStatus(), getAppStatus()])
    if (corpus.status === 'fulfilled') {
      status.value = corpus.value
    } else {
      error.value = (corpus.reason as Error).message || 'Could not load corpus status.'
    }
    appStatus.value = app.status === 'fulfilled' ? app.value : null
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="admin-tab-body">
    <div class="tab-toolbar">
      <span v-if="status" class="muted-text">Measured {{ formatDate(status.generated_at) }}</span>
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

    <div v-if="loading && !status" class="tab-loading">
      <ProgressSpinner style="width: 2rem; height: 2rem" />
    </div>

    <template v-else-if="status">
      <!--
        The operating model, stated where someone is looking at corpus counts and
        wondering why there is no "sync everything" button on this page.
      -->
      <Message v-if="remoteState === 'error'" severity="warn" :closable="false">
        SBP is not reachable from this instance, so the corpus cannot be updated here.
        Sync on a machine with an unblocked IP and re-upload — see
        <code>docs/DEPLOYMENT_PLAN.md</code> §2.1.
      </Message>

      <!-- One column per thing the corpus is made of. Headline, then what qualifies it. -->
      <div class="summary-columns">
        <Card class="glass-panel summary-column">
          <template #title>Circulars</template>
          <template #content>
            <p class="headline">
              {{ formatCount(circulars?.total) }}
              <span class="headline-unit">circulars</span>
            </p>
            <dl class="summary-list">
              <dt>Departments</dt>
              <dd>{{ formatCount(circulars?.by_department.length) }}</dd>
              <dt>Span</dt>
              <dd>{{ circularYears }}</dd>
              <dt>Newest</dt>
              <dd>
                {{ circulars?.latest?.reference || circulars?.latest?.title || ABSENT }}
                <span class="muted-text">{{ formatDate(circulars?.latest_date) }}</span>
              </dd>
              <dt>Attachments</dt>
              <dd>
                {{ formatCount(attachments?.total) }}
                <span class="muted-text">
                  {{ formatCount(attachments?.with_text) }} with text ·
                  {{ formatCount(attachments?.vectorized) }} vectorized
                </span>
              </dd>
              <dt>Indexed today</dt>
              <dd>{{ formatCount(circulars?.indexed_today) }}</dd>
            </dl>
          </template>
        </Card>

        <Card class="glass-panel summary-column">
          <template #title>Laws &amp; regulations</template>
          <template #content>
            <p class="headline">
              {{ formatCount(laws?.documents) }}
              <span class="headline-unit">documents</span>
            </p>
            <dl class="summary-list">
              <dt>Structure</dt>
              <dd>
                {{ formatCount(laws?.containers) }} collections ·
                {{ formatCount(laws?.parts) }} parts
              </dd>
              <dt>Versions</dt>
              <dd>
                {{ formatCount(laws?.versions) }}
                <span class="muted-text">{{ formatCount(laws?.current_versions) }} in force</span>
              </dd>
              <dt>Circular links</dt>
              <dd>{{ formatCount(laws?.circular_links) }}</dd>
              <dt>Awaiting content</dt>
              <dd>
                {{ formatCount(laws?.stubs) }} stubs
                <span class="muted-text">
                  {{ formatCount(laws?.external) }} external ·
                  {{ formatCount(laws?.circular_backed) }} are circulars
                </span>
              </dd>
              <dt>Indexed editions</dt>
              <dd>{{ formatCount(laws?.vectorized_versions) }}</dd>
            </dl>
          </template>
        </Card>

        <Card class="glass-panel summary-column">
          <template #title>AI analysis</template>
          <template #content>
            <p class="headline">
              {{ overallCoverage.percent }}%
              <span class="headline-unit">of all features generated</span>
            </p>
            <dl class="summary-list">
              <dt>Circulars touched</dt>
              <dd>
                {{ formatCount(circulars?.analysed) }}
                <span class="muted-text">of {{ formatCount(circulars?.total) }}</span>
              </dd>
              <dt>Editions touched</dt>
              <dd>
                {{ formatCount(laws?.analysed) }}
                <span class="muted-text">of {{ formatCount(laws?.current_versions) }} in force</span>
              </dd>
              <dt>Relationships</dt>
              <dd>
                {{ formatCount(status.relationships.total) }}
                <span class="muted-text">
                  {{ formatCount(unresolvedRelationships) }} name a circular we do not hold
                </span>
              </dd>
              <dt>Regulatory values</dt>
              <dd>{{ formatCount(status.entities.total) }}</dd>
              <dt>Consolidated chains</dt>
              <dd>
                {{ formatCount(status.consolidations.total) }}
                <span v-if="status.consolidations.stale" class="muted-text">
                  {{ formatCount(status.consolidations.stale) }} stale
                </span>
              </dd>
            </dl>
          </template>
        </Card>
      </div>

      <Card class="glass-panel">
        <template #title>Analysis coverage</template>
        <template #content>
          <div class="two-column">
            <div>
              <h3 class="section-label">Circulars</h3>
              <div class="coverage-stack">
                <AdminCoverageBar
                  v-for="row in circulars?.coverage || []"
                  :key="row.feature"
                  :feature="row.feature"
                  :generated="row.generated"
                  :total="row.total"
                />
              </div>
            </div>
            <div>
              <h3 class="section-label">Laws &amp; regulations</h3>
              <div class="coverage-stack">
                <AdminCoverageBar
                  v-for="row in laws?.coverage || []"
                  :key="row.feature"
                  :feature="row.feature"
                  :generated="row.generated"
                  :total="row.total"
                />
              </div>
            </div>
          </div>
          <p class="field-hint">
            Counted from each feature's generated-at timestamp, so a run that produced
            nothing still counts as done and does not read as a gap to re-run. Law coverage
            is measured against the edition in force, not the document: a new capture reads
            as un-analysed rather than showing wording no longer current.
          </p>
        </template>
      </Card>

      <!--
        Everything below is reference data — useful when you are chasing something
        specific, noise when you are not. Collapsed by default, and each header carries
        its own count so the fold still tells you whether it is worth opening.
      -->
      <Accordion v-model:value="openDetail" multiple class="detail-accordion">
        <AccordionPanel value="departments">
          <AccordionHeader>
            Circulars by department
            <span class="panel-count">{{ formatCount(circulars?.by_department.length) }}</span>
          </AccordionHeader>
          <AccordionContent>
            <DataTable
              :value="circulars?.by_department || []"
              size="small"
              scrollable
              scroll-height="20rem"
              class="facet-table"
            >
              <Column field="label" header="Department" />
              <Column field="count" header="Circulars" style="width: 8rem" />
              <template #empty>No circulars.</template>
            </DataTable>
          </AccordionContent>
        </AccordionPanel>

        <AccordionPanel value="years">
          <AccordionHeader>
            Circulars by year
            <span class="panel-count">{{ formatCount(circulars?.by_year.length) }}</span>
          </AccordionHeader>
          <AccordionContent>
            <DataTable
              :value="circulars?.by_year || []"
              size="small"
              scrollable
              scroll-height="20rem"
              class="facet-table"
            >
              <Column field="label" header="Year" />
              <Column field="count" header="Circulars" style="width: 8rem" />
              <template #empty>No circulars.</template>
            </DataTable>
          </AccordionContent>
        </AccordionPanel>

        <AccordionPanel value="lifecycle">
          <AccordionHeader>
            Circular lifecycle and relationship types
            <span class="panel-count">{{ formatCount(status.relationships.total) }} edges</span>
          </AccordionHeader>
          <AccordionContent>
            <div class="two-column">
              <div>
                <h3 class="section-label">Status</h3>
                <div class="chip-column">
                  <AdminStatusChip
                    v-for="row in circularStatusFacets"
                    :key="row.label"
                    :status="row.label"
                    :count="row.count"
                  />
                </div>
                <p class="field-hint">
                  Recomputed from relationships by <span class="mono">circulars status</span>,
                  not scraped.
                </p>
              </div>
              <div>
                <h3 class="section-label">Relationship types</h3>
                <div class="chip-column">
                  <AdminStatusChip
                    v-for="row in relationshipFacets"
                    :key="row.label"
                    :status="row.label"
                    :count="row.count"
                  />
                  <span v-if="!relationshipFacets.length" class="muted-text">None extracted.</span>
                </div>
                <p class="field-hint">
                  {{ formatCount(status.relationships.resolved) }} resolve to a circular in
                  the corpus; the rest name one we do not hold.
                </p>
              </div>
            </div>
          </AccordionContent>
        </AccordionPanel>

        <AccordionPanel value="attachments">
          <AccordionHeader>
            Attachment extraction
            <span class="panel-count">
              {{ formatCount(attachments?.with_error) }} errors
            </span>
          </AccordionHeader>
          <AccordionContent>
            <div class="chip-row">
              <AdminStatusChip
                v-for="row in extractionFacets"
                :key="row.label"
                :status="row.label"
                :count="row.count"
              />
              <span v-if="!extractionFacets.length" class="muted-text">No attachments.</span>
            </div>
            <p class="field-hint">
              An attachment with no extracted text is retrievable but not searchable — it
              contributes nothing to the index whatever its file is.
            </p>
          </AccordionContent>
        </AccordionPanel>

        <AccordionPanel value="laws">
          <AccordionHeader>
            Law types and extraction
            <span class="panel-count">{{ formatCount(laws?.versions) }} versions</span>
          </AccordionHeader>
          <AccordionContent>
            <div class="two-column">
              <div>
                <h3 class="section-label">By type</h3>
                <DataTable :value="laws?.by_doc_type || []" size="small" class="facet-table">
                  <Column field="label" header="Type" />
                  <Column field="count" header="Count" style="width: 6rem" />
                  <template #empty>No documents.</template>
                </DataTable>
              </div>
              <div>
                <h3 class="section-label">Extraction</h3>
                <div class="chip-column">
                  <AdminStatusChip
                    v-for="row in lawExtractionFacets"
                    :key="row.label"
                    :status="row.label"
                    :count="row.count"
                  />
                  <span v-if="!lawExtractionFacets.length" class="muted-text">No versions yet.</span>
                </div>
                <p class="field-hint">
                  Counted across every captured version, including superseded editions that
                  stay archived but drop out of the index.
                </p>
              </div>
            </div>
          </AccordionContent>
        </AccordionPanel>
      </Accordion>
    </template>
  </div>
</template>

