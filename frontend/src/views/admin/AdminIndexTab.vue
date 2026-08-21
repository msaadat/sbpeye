<script setup lang="ts">
/**
 * What the search index can and cannot currently reach.
 *
 * Two readings, deliberately kept apart. The columns are the *record* — what the ledger
 * says the last sync or audit wrote — and load instantly from SQLite. The audit below is
 * a *measurement*: it re-reads every chunk in the vector store and compares. They can
 * disagree, and the disagreement is the interesting part.
 *
 * The audit writes nothing (`reconcile(write=False)` on the server). Repairing a stale
 * index is `sbpeye inventory index --repair`, still a CLI action.
 */
import { computed, onMounted, ref } from 'vue'
import Accordion from 'primevue/accordion'
import AccordionContent from 'primevue/accordioncontent'
import AccordionHeader from 'primevue/accordionheader'
import AccordionPanel from 'primevue/accordionpanel'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Message from 'primevue/message'
import ProgressSpinner from 'primevue/progressspinner'

import AdminStatusChip from '@/components/AdminStatusChip.vue'
import {
  getAdminIndexStatus,
  runAdminIndexAudit,
  type AdminIndexAudit,
  type AdminIndexStatus,
} from '@/lib/api'
import { ABSENT, facetRows, formatCount, formatDate, humanize, percent } from './adminFormat'

// Which detail panels are open. A real model, not a literal `[]` on the Accordion:
// that binding hands the component a fresh empty array on every render, so a panel
// closes again the instant anything else on the page updates.
const openDetail = ref<string[]>([])

const status = ref<AdminIndexStatus | null>(null)
const audit = ref<AdminIndexAudit | null>(null)
const loading = ref(false)
const auditing = ref(false)
const error = ref('')
const auditError = ref('')

const ledgerFacets = computed(() => facetRows(status.value?.ledger.by_status))
const kindFacets = computed(() => facetRows(status.value?.ledger.by_source_kind))

/** Chunks the ledger expects but the last write did not produce. */
const chunkShortfall = computed(() => {
  const ledger = status.value?.ledger
  return ledger ? ledger.expected_chunks - ledger.indexed_chunks : 0
})

/** Share of ledger rows currently recorded as indexed — the headline for the record. */
const indexedShare = computed(() => {
  const ledger = status.value?.ledger
  if (!ledger) return 0
  return percent(ledger.by_status.indexed || 0, ledger.rows)
})

/**
 * Whether the recorded index was built with the embedding model now configured.
 *
 * The failure this catches is silent by nature: querying an index built by a different
 * model returns plausible-looking nonsense rather than an error (deployment plan §2.2).
 * `null` means nothing is recorded yet, which is unknown rather than wrong.
 */
const fingerprintMismatch = computed(() => status.value?.drift.fingerprint_matches === false)
const chunkerMismatch = computed(() => status.value?.drift.chunker_matches === false)

const storeTone = computed(() => {
  const state = status.value?.vector_store.state || ''
  if (state.startsWith('error')) return 'tone-error'
  if (state === 'empty') return 'tone-warn'
  return ''
})

const unsearchableFacets = computed(() => facetRows(audit.value?.unsearchable))
const excludedFacets = computed(() => facetRows(audit.value?.excluded_by_design))
const unsearchableTotal = computed(() =>
  unsearchableFacets.value.reduce((sum, row) => sum + row.count, 0),
)

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    status.value = await getAdminIndexStatus()
  } catch (err) {
    error.value = (err as Error).message || 'Could not load index status.'
  } finally {
    loading.value = false
  }
}

async function startAudit(): Promise<void> {
  auditing.value = true
  auditError.value = ''
  try {
    audit.value = await runAdminIndexAudit()
    // The ledger's own numbers may be older than what the audit just measured; reloading
    // keeps the two halves of the page describing the same moment.
    await load()
  } catch (err) {
    auditError.value = (err as Error).message || 'The audit could not complete.'
  } finally {
    auditing.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="admin-tab-body">
    <div class="tab-toolbar">
      <span v-if="status" class="muted-text">Read {{ formatDate(status.generated_at) }}</span>
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
      <Message v-if="fingerprintMismatch" severity="error" :closable="false">
        The index was built with a different embedding model than the one configured now
        (<span class="mono">{{ status.embedding.model }}</span>). Searches will return
        plausible-looking nonsense rather than failing. Either restore the original model
        or rebuild the index with <span class="mono">sbpeye reindex</span>.
      </Message>
      <Message v-else-if="chunkerMismatch" severity="warn" :closable="false">
        Part of the index was built by an older chunker
        ({{ status.drift.recorded_chunker_versions.join(', ') }}, now
        {{ status.embedding.chunker_version }}). Those sources read as stale until
        re-indexed.
      </Message>

      <div class="summary-columns">
        <Card class="glass-panel summary-column">
          <template #title>Ledger</template>
          <template #content>
            <p class="headline">
              {{ indexedShare }}%
              <span class="headline-unit">of sources recorded indexed</span>
            </p>
            <dl class="summary-list">
              <dt>Sources</dt>
              <dd>
                {{ formatCount(status.ledger.rows) }}
                <span class="muted-text">one row per physical source</span>
              </dd>
              <dt>Searchable</dt>
              <dd>
                {{ formatCount(status.ledger.searchable) }}
                <span class="muted-text">indexed or stale — known to the index</span>
              </dd>
              <dt>Chunk shortfall</dt>
              <dd>
                {{ formatCount(chunkShortfall) }}
                <span class="muted-text">
                  {{ formatCount(status.ledger.expected_chunks) }} expected ·
                  {{ formatCount(status.ledger.indexed_chunks) }} recorded
                </span>
              </dd>
              <dt>Last indexed</dt>
              <dd>{{ formatDate(status.ledger.last_indexed_at) }}</dd>
            </dl>
          </template>
        </Card>

        <Card class="glass-panel summary-column">
          <template #title>Stored indexes</template>
          <template #content>
            <p class="headline" :class="storeTone">
              {{ formatCount(status.vector_store.chunks) }}
              <span class="headline-unit">chunks in the vector store</span>
            </p>
            <dl class="summary-list">
              <dt>Store state</dt>
              <dd>{{ humanize(status.vector_store.state) }}</dd>
              <dt>Circular FTS</dt>
              <dd>
                {{ status.fts.circulars === null ? 'not built' : formatCount(status.fts.circulars) }}
              </dd>
              <dt>Law FTS</dt>
              <dd>{{ status.fts.laws === null ? 'not built' : formatCount(status.fts.laws) }}</dd>
              <dt>Sources with errors</dt>
              <dd>{{ formatCount(status.ledger.with_error) }}</dd>
            </dl>
          </template>
        </Card>

        <Card class="glass-panel summary-column">
          <template #title>Configuration</template>
          <template #content>
            <p class="headline" :class="fingerprintMismatch ? 'tone-error' : ''">
              {{ status.embedding.provider }}
              <span class="headline-unit">embedding backend</span>
            </p>
            <dl class="summary-list">
              <dt>Model</dt>
              <dd class="mono">{{ status.embedding.model }}</dd>
              <dt>Chunker</dt>
              <dd>{{ status.embedding.chunker_version }}</dd>
              <dt>Matches index</dt>
              <dd>
                <template v-if="status.drift.fingerprint_matches === null">
                  unknown — nothing recorded yet
                </template>
                <template v-else-if="status.drift.fingerprint_matches">
                  yes
                  <span class="muted-text">stored vectors are readable by this model</span>
                </template>
                <template v-else>
                  no — searches will return nonsense rather than fail
                </template>
              </dd>
            </dl>
          </template>
        </Card>
      </div>

      <Card class="glass-panel">
        <template #title>Reconcile against the store</template>
        <template #content>
          <p class="field-hint">
            Re-reads every chunk in the vector store and compares it with the corpus. This
            is a measurement, not a repair — it writes nothing, here or to the ledger.
            Expect a few seconds on a full corpus.
          </p>

          <div class="chip-row">
            <Button
              label="Run audit"
              icon="pi pi-search"
              size="small"
              :loading="auditing"
              @click="startAudit"
            />
            <span v-if="audit" class="muted-text">
              Took {{ (audit.duration_ms / 1000).toFixed(1) }} s ·
              {{ formatDate(audit.generated_at) }}
            </span>
          </div>

          <Message v-if="auditError" severity="error" :closable="false" class="audit-message">
            {{ auditError }}
          </Message>

          <template v-if="audit">
            <Message
              :severity="audit.is_complete ? 'success' : 'warn'"
              :closable="false"
              class="audit-message"
            >
              <template v-if="audit.is_complete">
                Every text-bearing source in scope is currently indexed.
              </template>
              <template v-else>
                The corpus has holes: an inventory search cannot claim to have seen
                everything until these are closed.
              </template>
            </Message>

            <div class="summary-columns audit-columns">
              <div class="audit-column">
                <h3 class="section-label">Measured ({{ formatCount(audit.searchable_sources) }} searchable)</h3>
                <!--
                  Spelled out rather than shown as a chip row over `status_counts`: that
                  row is exactly these two numbers plus the Unsearchable column beside it,
                  so it said everything twice.
                -->
                <dl class="summary-list">
                  <dt>Indexed</dt>
                  <dd>
                    {{ formatCount(audit.status_counts.indexed || 0) }}
                    <span class="muted-text">stored chunks match the current chunker</span>
                  </dd>
                  <dt>Stale</dt>
                  <dd>
                    {{ formatCount(audit.stale_sources) }}
                    <span class="muted-text">text and stored chunks disagree</span>
                  </dd>
                  <dt>Orphan chunks</dt>
                  <dd>
                    {{ formatCount(audit.orphan_chunks) }}
                    <span class="muted-text">stored, but no source vouches for them</span>
                  </dd>
                  <dt>Chunks</dt>
                  <dd>
                    {{ formatCount(audit.indexed_chunks) }}
                    <span class="muted-text">{{ formatCount(audit.expected_chunks) }} expected</span>
                  </dd>
                </dl>
              </div>

              <div class="audit-column">
                <h3 class="section-label">Unsearchable ({{ formatCount(unsearchableTotal) }})</h3>
                <div class="chip-column">
                  <AdminStatusChip
                    v-for="row in unsearchableFacets"
                    :key="row.label"
                    :status="row.label"
                    :count="row.count"
                  />
                  <span v-if="!unsearchableFacets.length" class="muted-text">None.</span>
                </div>
                <p class="field-hint">
                  Sources that hold text nothing can retrieve — a failed extraction, an
                  unsupported file type. Not broken indexing; missing content.
                </p>
              </div>

              <div class="audit-column">
                <h3 class="section-label">Excluded by design</h3>
                <div class="chip-column">
                  <span v-for="row in excludedFacets" :key="row.label" class="excluded-row">
                    {{ humanize(row.label) }}
                    <span class="muted-text">{{ row.count.toLocaleString() }}</span>
                  </span>
                  <span v-if="!excludedFacets.length" class="muted-text">None.</span>
                </div>
                <p class="field-hint">
                  Deliberately outside the index — superseded law editions, container
                  manifests. Their absence is correct, not a gap.
                </p>
              </div>
            </div>
          </template>
        </template>
      </Card>

      <Accordion v-model:value="openDetail" multiple class="detail-accordion">
        <AccordionPanel value="recorded">
          <AccordionHeader>
            Recorded status and source kinds
            <span class="panel-count">{{ formatCount(status.ledger.rows) }} rows</span>
          </AccordionHeader>
          <AccordionContent>
            <div class="two-column">
              <div>
                <h3 class="section-label">Status</h3>
                <div class="chip-column">
                  <AdminStatusChip
                    v-for="row in ledgerFacets"
                    :key="row.label"
                    :status="row.label"
                    :count="row.count"
                  />
                  <span v-if="!ledgerFacets.length" class="muted-text">
                    The ledger is empty — nothing has been indexed or audited yet.
                  </span>
                </div>
                <p class="field-hint">
                  A source counts as indexed only when the number of stored chunks matches
                  what the current chunker produces from its text. That is what makes the
                  count a claim about searchability rather than about a boolean someone
                  once set.
                </p>
              </div>
              <div>
                <h3 class="section-label">Source kind</h3>
                <div class="chip-column">
                  <AdminStatusChip
                    v-for="row in kindFacets"
                    :key="row.label"
                    :status="row.label"
                    :count="row.count"
                  />
                </div>
              </div>
            </div>
          </AccordionContent>
        </AccordionPanel>

        <AccordionPanel value="fingerprints">
          <AccordionHeader>
            Fingerprints and paths
            <span class="panel-count">{{ status.embedding.chunker_version }}</span>
          </AccordionHeader>
          <AccordionContent>
            <dl class="kv-list">
              <dt>Configured fingerprint</dt>
              <dd class="mono">{{ status.embedding.fingerprint }}</dd>
              <dt>Recorded in ledger</dt>
              <dd class="mono">
                {{ status.drift.recorded_fingerprints.join(', ') || 'none recorded' }}
              </dd>
              <dt>Recorded chunkers</dt>
              <dd>{{ status.drift.recorded_chunker_versions.join(', ') || 'none recorded' }}</dd>
              <dt>Store path</dt>
              <dd class="mono">{{ status.vector_store.path }}</dd>
              <dt v-if="audit">Last audited against</dt>
              <dd v-if="audit" class="mono">
                {{ audit.embedding_fingerprint }} · {{ audit.chunker_version || ABSENT }}
              </dd>
            </dl>
            <p class="field-hint">
              More than one recorded fingerprint means the ledger describes an index built
              in more than one configuration — a partial re-index, not a clean one.
            </p>
          </AccordionContent>
        </AccordionPanel>
      </Accordion>
    </template>
  </div>
</template>

<style scoped>
.audit-message {
  margin-top: 1rem;
}

.audit-columns {
  margin-top: 1.25rem;
}

/* Plain columns inside a card, not cards of their own — nesting panels inside the audit
   panel gave every number a second border to sit behind. */
.audit-column {
  min-width: 0;
}

.excluded-row {
  display: flex;
  gap: 0.5rem;
  font-size: var(--sbp-fs-meta);
}
</style>
