<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Message from 'primevue/message'
import Popover from 'primevue/popover'
import ProgressSpinner from 'primevue/progressspinner'
import Tag from 'primevue/tag'
import {
  getAIGenerationJob,
  buildDocumentContentUrl,
  getCircularDetail,
  getCircularSource,
  refreshCircular as refreshCircularSource,
  startCircularGeneration,
  type AIGenerationJob,
  type ApiError,
  type CircularDetail,
  type CircularAttachment,
  type ComplianceChecklistItem,
  type GenerationAction,
  type GenerationFeature,
  type CircularRelationship,
  type CircularRelationshipTarget,
  type CircularSourceContent,
} from '@/lib/api'

const PdfPreviewDialog = defineAsyncComponent(() => import('@/components/PdfPreviewDialog.vue'))

type PreviewAttachment = Pick<CircularAttachment, 'id' | 'filename' | 'file_type'>

const props = defineProps<{ id: string }>()
const emit = defineEmits<{ close: [] }>()
const router = useRouter()
const toast = useToast()

const circular = ref<CircularDetail | null>(null)
const source = ref<CircularSourceContent | null>(null)
const loading = ref(false)
const sourceLoading = ref(false)
const errorMessage = ref('')
const sourceError = ref('')
const refreshingSource = ref(false)
const pdfDialogVisible = ref(false)
const attachmentDialogVisible = ref(false)
const selectedAttachment = ref<PreviewAttachment | null>(null)
const summaryExpanded = ref(false)
const checklistExpanded = ref(false)
const generationPopover = ref<InstanceType<typeof Popover> | null>(null)
const activeJob = ref<AIGenerationJob | null>(null)
let pollTimer: ReturnType<typeof setTimeout> | null = null
let pollEpoch = 0

const generationFeatures: Array<{ feature: GenerationFeature; label: string; icon: string }> = [
  { feature: 'summary', label: 'Summary', icon: 'pi pi-align-left' },
  { feature: 'tags', label: 'Tags', icon: 'pi pi-tags' },
  { feature: 'checklist', label: 'Checklist', icon: 'pi pi-list-check' },
  { feature: 'relationships', label: 'Relationships', icon: 'pi pi-share-alt' },
]

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

function openAttachment(attachment: PreviewAttachment) {
  if (attachment.file_type?.toLowerCase() === 'pdf') {
    selectedAttachment.value = attachment
    attachmentDialogVisible.value = true
    return
  }
  window.open(buildDocumentContentUrl(attachment.id), '_blank', 'noopener,noreferrer')
}

function handleSourceClick(event: MouseEvent) {
  const target = event.target instanceof Element ? event.target.closest<HTMLAnchorElement>('a[data-document-link="true"]') : null
  if (!target) return
  event.preventDefault()
  const href = target.getAttribute('href') || '/'
  if (href.startsWith('/documents/open')) {
    const id = new URL(href, window.location.origin).searchParams.get('id')
    const attachment = circular.value?.attachments.find((item) => item.id === id)
    if (attachment) {
      openAttachment(attachment)
    } else if (id) {
      openAttachment({
        id,
        filename: target.textContent?.trim() || 'Attachment',
        file_type: target.dataset.documentKind?.toLowerCase() || null,
      })
    }
    return
  }
  void router.push(href)
}

function handoffToChat() {
  void router.push({ path: '/chat', query: { circular_ids: props.id } })
}

function checklistText(item: ComplianceChecklistItem | string): string {
  return typeof item === 'string' ? item : item.item
}

function checklistNeedsAction(item: ComplianceChecklistItem | string): boolean {
  return typeof item !== 'string' && item.action_required
}

function hasGenerated(feature: GenerationFeature): boolean {
  return Boolean(circular.value?.generation?.[feature])
}

function generationLabel(feature: GenerationFeature, label: string): string {
  return `${hasGenerated(feature) ? 'Regenerate' : 'Generate'} ${label}`
}

const allGenerated = computed(() => generationFeatures.every(({ feature }) => hasGenerated(feature)))

function stopPolling() {
  pollEpoch += 1
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = null
}

async function refreshCircular() {
  circular.value = await getCircularDetail(props.id)
}

async function pollGeneration(jobId: string, epoch: number) {
  if (epoch !== pollEpoch) return
  try {
    const job = await getAIGenerationJob(jobId)
    if (epoch !== pollEpoch) return
    activeJob.value = job
    if (job.status === 'succeeded') {
      await refreshCircular()
      activeJob.value = null
      toast.add({ severity: 'success', summary: 'AI analysis complete', detail: 'The circular intelligence was updated.', life: 3500 })
      return
    }
    if (job.status === 'failed') {
      activeJob.value = null
      toast.add({ severity: 'error', summary: 'AI generation failed', detail: job.error || 'The background job failed.', life: 6000 })
      return
    }
    pollTimer = setTimeout(() => void pollGeneration(jobId, epoch), 1000)
  } catch (error) {
    activeJob.value = null
    toast.add({ severity: 'error', summary: 'Job status unavailable', detail: error instanceof Error ? error.message : 'Unable to check generation status.', life: 5000 })
  }
}

async function generate(feature: GenerationAction) {
  generationPopover.value?.hide()
  if (activeJob.value) return
  stopPolling()
  const epoch = pollEpoch
  try {
    const job = await startCircularGeneration(props.id, feature)
    activeJob.value = job
    void pollGeneration(job.id, epoch)
  } catch (error) {
    const apiError = error as ApiError
    const existingJob = apiError.payload?.job as AIGenerationJob | undefined
    if (apiError.status === 409 && existingJob?.id) {
      activeJob.value = existingJob
      void pollGeneration(existingJob.id, epoch)
      return
    }
    toast.add({ severity: 'error', summary: 'Unable to start generation', detail: error instanceof Error ? error.message : 'The request failed.', life: 5000 })
  }
}

async function loadCircular() {
  stopPolling()
  activeJob.value = null
  summaryExpanded.value = false
  checklistExpanded.value = false
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

async function refreshFromSbp() {
  refreshingSource.value = true
  try {
    await refreshCircularSource(props.id)
    await loadCircular()
    toast.add({ severity: 'success', summary: 'Circular refreshed', detail: 'The local copy was updated from SBP.', life: 3500 })
  } catch (error) {
    toast.add({ severity: 'error', summary: 'Refresh failed', detail: error instanceof Error ? error.message : 'Unable to refresh the circular.', life: 6000 })
  } finally {
    refreshingSource.value = false
  }
}

onMounted(loadCircular)
watch(() => props.id, loadCircular)
onBeforeUnmount(stopPolling)
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
            <span v-for="item in circular.tags" :key="item" class="intelligence-pill tag-pill header-tag-pill">{{ item }}</span>
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
            <Button
              icon="pi pi-sparkles"
              text
              rounded
              severity="help"
              :loading="Boolean(activeJob)"
              aria-label="Generate AI analysis"
              :title="activeJob ? `Generating ${activeJob.feature}` : 'Generate AI analysis'"
              @click="generationPopover?.toggle($event)"
            />
            <Button v-if="isPdf" icon="pi pi-file-pdf" text rounded severity="danger" aria-label="Preview PDF" title="Preview PDF" @click="pdfDialogVisible = true" />
            <Button icon="pi pi-refresh" text rounded severity="secondary" :loading="refreshingSource" aria-label="Refresh from SBP" title="Refresh local copy from SBP" @click="refreshFromSbp" />
            <Button icon="pi pi-comments" text rounded severity="contrast" aria-label="Open in chat" title="Open in chat" @click="handoffToChat" />
          </div>
        </div>
        <div v-if="activeJob" class="generation-progress" role="status">
          <i class="pi pi-sparkles" /> Generating {{ activeJob.feature === 'all' ? 'all AI analysis' : activeJob.feature }} in the background
        </div>
      </header>

      <Popover ref="generationPopover" class="generation-popover">
        <div class="generation-menu">
          <span class="generation-menu-title">AI analysis</span>
          <Button
            v-for="item in generationFeatures"
            :key="item.feature"
            :icon="item.icon"
            :label="generationLabel(item.feature, item.label)"
            text
            size="small"
            :disabled="Boolean(activeJob)"
            @click="generate(item.feature)"
          />
          <div class="generation-menu-divider" />
          <Button
            icon="pi pi-sparkles"
            :label="allGenerated ? 'Regenerate All' : 'Generate All'"
            size="small"
            :disabled="Boolean(activeJob)"
            @click="generate('all')"
          />
        </div>
      </Popover>

      <section v-if="circular.summary" class="detail-section summary-section">
        <h2>
          <button
            type="button"
            class="collapsible-heading"
            :aria-expanded="summaryExpanded"
            @click="summaryExpanded = !summaryExpanded"
          >
            <span>Summary</span>
            <i :class="summaryExpanded ? 'pi pi-chevron-up' : 'pi pi-chevron-down'" />
          </button>
        </h2>
        <p v-show="summaryExpanded" class="detail-copy">{{ circular.summary }}</p>
      </section>

      <section
        v-if="circular.compliance_checklist?.length || circular.relationships.outgoing.length || circular.relationships.incoming.length"
        class="detail-section intelligence-section"
      >
        <div v-if="circular.compliance_checklist?.length" class="pill-group">
          <h2>
            <button
              type="button"
              class="collapsible-heading"
              :aria-expanded="checklistExpanded"
              @click="checklistExpanded = !checklistExpanded"
            >
              <span>Checklist</span>
              <i :class="checklistExpanded ? 'pi pi-chevron-up' : 'pi pi-chevron-down'" />
            </button>
          </h2>
          <div v-show="checklistExpanded" class="intelligence-pills">
            <span v-for="(item, index) in circular.compliance_checklist" :key="`${checklistText(item)}-${index}`" class="intelligence-pill checklist-pill">
              <i :class="checklistNeedsAction(item) ? 'pi pi-exclamation-circle action-required' : 'pi pi-check-circle'" /> {{ checklistText(item) }}
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

      <section v-if="circular.attachments.length" class="detail-section">
        <h2>Documents</h2>
        <div class="document-pills">
          <button
            v-for="attachment in circular.attachments"
            :key="attachment.id"
            type="button"
            class="document-pill"
            @click="openAttachment(attachment)"
          >
            <i :class="attachment.file_type === 'pdf' ? 'pi pi-file-pdf' : 'pi pi-file'" />
            <span>{{ attachment.filename }}</span>
          </button>
        </div>
      </section>

      <section v-if="sourceLoading || sourceError || (source?.type === 'html' && source.content) || isPdf" class="detail-section source-section">
        <h2>Source content</h2>
        <Message v-if="sourceError" severity="warn" :closable="false">{{ sourceError }}</Message>
        <div v-if="sourceLoading" class="preview-loading compact-loading"><ProgressSpinner /><span>Loading source</span></div>
        <div v-else-if="source?.type === 'html' && source.content" class="source-frame">
          <div class="sbp-source-content" v-html="source.content" @click="handleSourceClick" />
        </div>
        <button v-else-if="isPdf" type="button" class="pdf-source-compact" @click="pdfDialogVisible = true">
          <i class="pi pi-file-pdf" /><span><strong>PDF source</strong><small>Open the document preview</small></span><i class="pi pi-angle-right" />
        </button>
      </section>
    </div>

    <PdfPreviewDialog v-model:visible="pdfDialogVisible" :title="circular?.title || 'Circular'" :url="sourceUrl" />
    <PdfPreviewDialog
      v-if="selectedAttachment"
      v-model:visible="attachmentDialogVisible"
      :title="selectedAttachment.filename"
      :document-id="selectedAttachment.id"
    />
  </aside>
</template>
