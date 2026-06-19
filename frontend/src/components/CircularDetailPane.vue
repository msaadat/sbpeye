<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import Button from 'primevue/button'
import Message from 'primevue/message'
import ProgressSpinner from 'primevue/progressspinner'
import Tag from 'primevue/tag'
import {
  getCircularDetail,
  getCircularSource,
  type CircularDetail,
  type CircularRelationship,
  type CircularRelationshipTarget,
  type CircularSourceContent,
} from '@/lib/api'

const PdfPreviewDialog = defineAsyncComponent(() => import('@/components/PdfPreviewDialog.vue'))

const props = defineProps<{ id: string }>()
const emit = defineEmits<{ close: [] }>()
const router = useRouter()

const circular = ref<CircularDetail | null>(null)
const source = ref<CircularSourceContent | null>(null)
const loading = ref(false)
const sourceLoading = ref(false)
const errorMessage = ref('')
const sourceError = ref('')
const pdfDialogVisible = ref(false)

const sourceUrl = computed(() => source.value?.url || circular.value?.url || '')
const isPdf = computed(() => source.value?.type === 'pdf' || sourceUrl.value.toLowerCase().split('?', 1)[0].endsWith('.pdf'))

function formatDate(value?: string | null): string {
  if (!value) return ''
  return new Intl.DateTimeFormat(undefined, { year: 'numeric', month: 'short', day: '2-digit' }).format(new Date(value))
}

function statusSeverity(status?: string | null): 'success' | 'info' | 'warn' | 'danger' | 'secondary' {
  const value = (status || '').toLowerCase()
  if (value.includes('active') || value.includes('indexed')) return 'success'
  if (value.includes('superseded') || value.includes('replaced')) return 'warn'
  if (value.includes('withdrawn') || value.includes('cancel')) return 'danger'
  return status ? 'info' : 'secondary'
}

function relationshipLabel(value?: string | null): string {
  return (value || 'Related').replace(/[_-]+/g, ' ').replace(/\b\w/g, (match) => match.toUpperCase())
}

function relationTarget(relation: CircularRelationship, direction: 'outgoing' | 'incoming'): CircularRelationshipTarget | null {
  return direction === 'incoming' ? relation.source || null : relation.target || null
}

function unresolvedReference(relation: CircularRelationship, direction: 'outgoing' | 'incoming'): string {
  return direction === 'incoming'
    ? relation.source_id || 'Unresolved source circular'
    : relation.target_reference || relation.target_id || 'Unresolved target circular'
}

function openRelationship(id?: string | null) {
  if (!id) return
  void router.push({ path: `/circulars/${id}`, query: router.currentRoute.value.query })
}

function handoffToChat() {
  void router.push({ path: '/chat', query: { circular_ids: props.id } })
}

async function loadCircular() {
  loading.value = true
  sourceLoading.value = true
  errorMessage.value = ''
  sourceError.value = ''
  circular.value = null
  source.value = null

  try {
    circular.value = await getCircularDetail(props.id)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'Unable to load circular detail.'
    loading.value = false
    sourceLoading.value = false
    return
  } finally {
    loading.value = false
  }

  try {
    source.value = await getCircularSource(props.id)
    if (source.value.error) sourceError.value = source.value.error
  } catch (error) {
    sourceError.value = error instanceof Error ? error.message : 'Unable to load circular source.'
  } finally {
    sourceLoading.value = false
  }
}

onMounted(loadCircular)
watch(() => props.id, loadCircular)
</script>

<template>
  <aside class="circular-detail-pane" aria-label="Circular detail">
    <div v-if="loading" class="preview-loading compact-loading">
      <ProgressSpinner aria-label="Loading circular detail" />
      <span>Loading circular</span>
    </div>

    <Message v-else-if="errorMessage" severity="error" :closable="false">{{ errorMessage }}</Message>

    <div v-else-if="circular" class="detail-pane-scroll">
      <header class="detail-document-header">
        <div class="detail-header-topline">
          <div class="detail-badges">
            <Tag v-if="circular.reference" :value="circular.reference" severity="secondary" />
            <Tag v-if="circular.status" :value="circular.status" :severity="statusSeverity(circular.status)" />
          </div>
          <Button icon="pi pi-times" text rounded aria-label="Close circular" title="Close" @click="emit('close')" />
        </div>
        <h1>{{ circular.title }}</h1>
        <div class="detail-meta-actions">
          <div class="detail-inline-meta">
            <span v-if="circular.department"><i class="pi pi-building" /> {{ circular.department }}</span>
            <span v-if="circular.date"><i class="pi pi-calendar" /> {{ formatDate(circular.date) }}</span>
          </div>
          <div class="detail-actions">
            <Button v-if="isPdf" icon="pi pi-file-pdf" text rounded severity="danger" aria-label="Preview PDF" title="Preview PDF" @click="pdfDialogVisible = true" />
            <Button v-if="circular.url" as="a" :href="circular.url" target="_blank" rel="noreferrer" icon="pi pi-external-link" text rounded severity="secondary" aria-label="Open source" title="Open source" />
            <Button icon="pi pi-comments" text rounded severity="contrast" aria-label="Open in chat" title="Open in chat" @click="handoffToChat" />
          </div>
        </div>
      </header>

      <section v-if="circular.summary" class="detail-section summary-section">
        <h2>Summary</h2>
        <p class="detail-copy">{{ circular.summary }}</p>
      </section>

      <section
        v-if="circular.tags?.length || circular.compliance_checklist?.length || circular.relationships.outgoing.length || circular.relationships.incoming.length"
        class="detail-section intelligence-section"
      >
        <div v-if="circular.tags?.length" class="pill-group">
          <h2>Tags</h2>
          <div class="intelligence-pills">
            <span v-for="item in circular.tags" :key="item" class="intelligence-pill tag-pill">{{ item }}</span>
          </div>
        </div>
        <div v-if="circular.compliance_checklist?.length" class="pill-group">
          <h2>Checklist</h2>
          <div class="intelligence-pills">
            <span v-for="item in circular.compliance_checklist" :key="item" class="intelligence-pill checklist-pill">
              <i class="pi pi-check-circle" /> {{ item }}
            </span>
          </div>
        </div>
        <div v-if="circular.relationships.outgoing.length || circular.relationships.incoming.length" class="pill-group">
          <h2>Relationships</h2>
          <div class="intelligence-pills">
            <button
              v-for="relation in circular.relationships.outgoing"
              :key="`out-${relation.type}-${relation.target_id || relation.target_reference}`"
              type="button"
              class="intelligence-pill relationship-pill"
              :disabled="!relationTarget(relation, 'outgoing')?.id"
              @click="openRelationship(relationTarget(relation, 'outgoing')?.id)"
            >
              <strong>{{ relationshipLabel(relation.type) }}</strong>
              {{ relationTarget(relation, 'outgoing')?.reference || relationTarget(relation, 'outgoing')?.title || unresolvedReference(relation, 'outgoing') }}
            </button>
            <button
              v-for="relation in circular.relationships.incoming"
              :key="`in-${relation.type}-${relation.source_id}`"
              type="button"
              class="intelligence-pill relationship-pill incoming-pill"
              :disabled="!relationTarget(relation, 'incoming')?.id"
              @click="openRelationship(relationTarget(relation, 'incoming')?.id)"
            >
              <strong>{{ relationshipLabel(relation.type) }}</strong>
              {{ relationTarget(relation, 'incoming')?.reference || relationTarget(relation, 'incoming')?.title || unresolvedReference(relation, 'incoming') }}
            </button>
          </div>
        </div>
      </section>

      <section v-if="sourceLoading || sourceError || (source?.type === 'html' && source.content) || isPdf" class="detail-section source-section">
        <h2>Source content</h2>
        <Message v-if="sourceError" severity="warn" :closable="false">{{ sourceError }}</Message>
        <div v-if="sourceLoading" class="preview-loading compact-loading"><ProgressSpinner /><span>Loading source</span></div>
        <div v-else-if="source?.type === 'html' && source.content" class="source-frame">
          <div class="sbp-source-content" v-html="source.content" />
        </div>
        <button v-else-if="isPdf" type="button" class="pdf-source-compact" @click="pdfDialogVisible = true">
          <i class="pi pi-file-pdf" /><span><strong>PDF source</strong><small>Open the document preview</small></span><i class="pi pi-angle-right" />
        </button>
      </section>
    </div>

    <PdfPreviewDialog v-model:visible="pdfDialogVisible" :title="circular?.title || 'Circular'" :url="sourceUrl" />
  </aside>
</template>
