<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import Message from 'primevue/message'
import Popover from 'primevue/popover'
import ProgressSpinner from 'primevue/progressspinner'
import {
  getAIGenerationJob,
  buildDocumentContentUrl,
  getCircularDetail,
  getCircularSource,
  downloadChecklistExcel,
  refreshCircular as refreshCircularSource,
  startCircularGeneration,
  type AIGenerationJob,
  type ApiError,
  type CircularDetail,
  type CircularAttachment,
  type CircularEntity,
  type GenerationAction,
  type GenerationFeature,
  type CircularRelationship,
  type CircularRelationshipTarget,
  type CircularSourceContent,
} from '@/lib/api'

const PdfPreviewDialog = defineAsyncComponent(() => import('@/components/PdfPreviewDialog.vue'))
const CircularGraph = defineAsyncComponent(() => import('@/components/CircularGraph.vue'))

type PreviewAttachment = Pick<CircularAttachment, 'id' | 'filename' | 'file_type'>

const props = defineProps<{ id: string, isPinned: boolean, pinPending: boolean }>()
const emit = defineEmits<{ close: [], 'toggle-pin': [] }>()
const router = useRouter()
const toast = useToast()

marked.use({
  breaks: true,
  gfm: true,
})

const circular = ref<CircularDetail | null>(null)
const source = ref<CircularSourceContent | null>(null)
const loading = ref(false)
const sourceLoading = ref(false)
const errorMessage = ref('')
const sourceError = ref('')
const refreshingSource = ref(false)
const exportingChecklist = ref(false)
const pdfDialogVisible = ref(false)
const attachmentDialogVisible = ref(false)
const selectedAttachment = ref<PreviewAttachment | null>(null)
const summaryExpanded = ref(false)
const generationPopover = ref<InstanceType<typeof Popover> | null>(null)
const activeJob = ref<AIGenerationJob | null>(null)
const graphVisible = ref(false)
const detailTab = ref<'document' | 'details'>('document')
let pollTimer: ReturnType<typeof setTimeout> | null = null
let pollEpoch = 0

const generationFeatures: Array<{ feature: GenerationFeature; label: string; icon: string }> = [
  { feature: 'summary', label: 'Summary', icon: 'pi pi-align-left' },
  { feature: 'tags', label: 'Tags', icon: 'pi pi-tags' },
  { feature: 'checklist', label: 'Checklist', icon: 'pi pi-list-check' },
  { feature: 'relationships', label: 'Relationships', icon: 'pi pi-share-alt' },
  { feature: 'entities', label: 'Regulatory Values', icon: 'pi pi-percentage' },
]

const ENTITY_TYPE_LABELS: Record<string, string> = {
  ratio: 'Ratios',
  monetary_threshold: 'Monetary thresholds',
  percentage_limit: 'Percentage limits',
  numeric_limit: 'Numeric limits',
  deadline: 'Deadlines',
  effective_date: 'Effective dates',
}

const ENTITY_TYPE_ORDER = [
  'ratio',
  'monetary_threshold',
  'percentage_limit',
  'numeric_limit',
  'deadline',
  'effective_date',
]

const COMPARATOR_PREFIX: Record<string, string> = {
  min: '≥ ',
  max: '≤ ',
  exactly: '',
  range: '',
}

interface EntityGroup {
  type: string
  label: string
  items: CircularEntity[]
}

const entityGroups = computed<EntityGroup[]>(() => {
  const entities = circular.value?.entities ?? []
  const byType = new Map<string, CircularEntity[]>()
  for (const entity of entities) {
    const list = byType.get(entity.entity_type) ?? []
    list.push(entity)
    byType.set(entity.entity_type, list)
  }
  return ENTITY_TYPE_ORDER.filter((type) => byType.has(type)).map((type) => ({
    type,
    label: ENTITY_TYPE_LABELS[type] ?? type,
    items: byType.get(type) as CircularEntity[],
  }))
})

function formatEntityValue(entity: CircularEntity): string {
  if (entity.value_text) {
    const prefix = entity.comparator ? COMPARATOR_PREFIX[entity.comparator] ?? '' : ''
    return `${prefix}${entity.value_text}`.trim()
  }
  if (entity.effective_date) return formatDate(entity.effective_date)
  return entity.value_numeric != null ? String(entity.value_numeric) : '—'
}

const sourceUrl = computed(() => source.value?.url || circular.value?.url || '')
const sourceWebsiteUrl = computed(() => source.value?.original_url || circular.value?.url || source.value?.url || '')
const isPdf = computed(() => source.value?.type === 'pdf' || sourceUrl.value.toLowerCase().split('?', 1)[0].endsWith('.pdf'))

function formatDate(value?: string | null): string {
  if (!value) return ''
  return new Intl.DateTimeFormat(undefined, { year: 'numeric', month: 'short', day: '2-digit' }).format(new Date(value))
}

function renderMarkdown(content?: string | null): string {
  if (!content) return ''
  return DOMPurify.sanitize(marked.parse(content) as string, {
    USE_PROFILES: { html: true },
  })
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

const INCOMING_LABELS: Record<string, string> = {
  supersedes: 'Superseded by',
  amends: 'Amended by',
  cancels: 'Cancelled by',
  clarifies: 'Clarified by',
  adds_to: 'Added to by',
}

const TYPE_ORDER = ['supersedes', 'amends', 'cancels', 'clarifies', 'adds_to']
const COLLAPSE_THRESHOLD = 12

interface RelationGroupItem {
  id: string | null
  label: string
}

interface RelationGroup {
  key: string
  direction: 'outgoing' | 'incoming'
  type: string
  label: string
  items: RelationGroupItem[]
}

const expandedGroups = ref<Set<string>>(new Set())

function toggleGroup(key: string) {
  const next = new Set(expandedGroups.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  expandedGroups.value = next
}

function relationTarget(relation: CircularRelationship, direction: 'outgoing' | 'incoming'): CircularRelationshipTarget | null {
  return direction === 'incoming' ? relation.source || null : relation.target || null
}

function unresolvedReference(relation: CircularRelationship, direction: 'outgoing' | 'incoming'): string {
  return direction === 'incoming'
    ? relation.source_id || 'Unresolved source circular'
    : relation.target_reference || relation.target_id || 'Unresolved target circular'
}

function buildGroups(relations: CircularRelationship[], direction: 'outgoing' | 'incoming'): RelationGroup[] {
  const groups = new Map<string, RelationGroup>()
  for (const relation of relations) {
    const type = relation.type || 'related'
    const key = `${direction}:${type}`
    let group = groups.get(key)
    if (!group) {
      group = {
        key,
        direction,
        type,
        label: direction === 'incoming' ? INCOMING_LABELS[type] ?? relationshipLabel(type) : relationshipLabel(type),
        items: [],
      }
      groups.set(key, group)
    }
    const target = relationTarget(relation, direction)
    group.items.push({
      id: target?.id ?? null,
      label: target?.reference || target?.title || unresolvedReference(relation, direction),
    })
  }
  return [...groups.values()]
}

const relationshipGroups = computed<RelationGroup[]>(() => {
  const relationships = circular.value?.relationships
  if (!relationships) return []
  const groups = [
    ...buildGroups(relationships.outgoing, 'outgoing'),
    ...buildGroups(relationships.incoming, 'incoming'),
  ]
  const rank = (type: string) => {
    const index = TYPE_ORDER.indexOf(type)
    return index === -1 ? TYPE_ORDER.length : index
  }
  return groups.sort((a, b) => {
    if (a.direction !== b.direction) return a.direction === 'outgoing' ? -1 : 1
    return rank(a.type) - rank(b.type)
  })
})

function visibleItems(group: RelationGroup): RelationGroupItem[] {
  if (expandedGroups.value.has(group.key) || group.items.length <= COLLAPSE_THRESHOLD) return group.items
  return group.items.slice(0, COLLAPSE_THRESHOLD)
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

function hasGenerated(feature: GenerationFeature): boolean {
  return Boolean(circular.value?.generation?.[feature])
}

function generationLabel(feature: GenerationFeature, label: string): string {
  return `${hasGenerated(feature) ? 'Regenerate' : 'Generate'} ${label}`
}

const allGenerated = computed(() => generationFeatures.every(({ feature }) => hasGenerated(feature)))
const hasRelationships = computed(() =>
  Boolean(circular.value?.relationships.outgoing.length || circular.value?.relationships.incoming.length),
)
const hasIntelligence = computed(() =>
  Boolean(
    circular.value?.summary ||
      hasRelationships.value ||
      entityGroups.value.length ||
      circular.value?.attachments.length,
  ),
)

function navigateFromGraph(id: string) {
  graphVisible.value = false
  void router.push({ path: `/circulars/${id}`, query: router.currentRoute.value.query })
}

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
      const hasGaps = job.result_status === 'completed_with_gaps'
      toast.add({
        severity: hasGaps ? 'warn' : 'success',
        summary: hasGaps ? 'AI analysis completed with gaps' : 'AI analysis complete',
        detail: hasGaps ? 'Some PDF attachments could not be analyzed.' : 'The circular intelligence was updated.',
        life: hasGaps ? 6000 : 3500,
      })
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
  detailTab.value = 'document'
  expandedGroups.value = new Set()
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

async function exportChecklist() {
  if (!circular.value?.compliance_checklist) return
  exportingChecklist.value = true
  try {
    await downloadChecklistExcel(circular.value.id, circular.value.reference)
  } catch (error) {
    toast.add({ severity: 'error', summary: 'Export failed', detail: error instanceof Error ? error.message : 'Unable to export the checklist.', life: 5000 })
  } finally {
    exportingChecklist.value = false
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

    <div v-else-if="circular" class="detail-pane-layout">
      <header class="detail-document-header">
        <div class="detail-header-topline">
          <div class="detail-badges">
            <span v-if="circular.reference" class="detail-eyebrow">{{ circular.reference }}</span>
            <span
              v-if="circular.status"
              class="status-chip"
              :class="`status-${statusSeverity(circular.status)}`"
            >
              <span class="status-dot" />{{ circular.status }}
            </span>
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
            <Button
              :icon="isPinned ? 'pi pi-bookmark-fill' : 'pi pi-bookmark'"
              text
              rounded
              severity="secondary"
              :loading="pinPending"
              :aria-label="isPinned ? 'Unpin circular' : 'Pin circular'"
              :title="isPinned ? 'Unpin circular' : 'Pin circular'"
              @click="emit('toggle-pin')"
            />
            <Button v-if="isPdf" icon="pi pi-file-pdf" text rounded severity="danger" aria-label="Preview PDF" title="Preview PDF" @click="pdfDialogVisible = true" />
            <Button
              v-if="circular.compliance_checklist"
              icon="pi pi-file-excel"
              text
              rounded
              severity="success"
              :loading="exportingChecklist"
              aria-label="Open checklist Excel file"
              title="Open checklist Excel file"
              @click="exportChecklist"
            />
            <Button
              v-if="sourceWebsiteUrl"
              as="a"
              :href="sourceWebsiteUrl"
              target="_blank"
              rel="noopener noreferrer"
              icon="pi pi-external-link"
              text
              rounded
              severity="info"
              aria-label="View on SBP website"
              title="View on SBP website"
            />
            <Button icon="pi pi-refresh" text rounded severity="secondary" :loading="refreshingSource" aria-label="Refresh from SBP" title="Refresh local copy from SBP" @click="refreshFromSbp" />
            <Button
              v-if="hasRelationships"
              icon="pi pi-sitemap"
              text
              rounded
              severity="secondary"
              aria-label="View relationship graph"
              title="Related circulars"
              @click="graphVisible = true"
            />
            <span class="detail-actions-sep" aria-hidden="true" />
            <Button icon="pi pi-comments" text rounded severity="contrast" aria-label="Open in chat" title="Open in chat" @click="handoffToChat" />
          </div>
        </div>
        <div v-if="activeJob" class="generation-progress" role="status">
          <i class="pi pi-sparkles" />
          Generating {{ activeJob.feature === 'all' ? 'all AI analysis' : activeJob.feature }} in the background
          <span v-if="activeJob.progress_total">
            ({{ activeJob.progress_completed }}/{{ activeJob.progress_total }} source units)
          </span>
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

      <div v-if="hasIntelligence" class="detail-tabbar" role="tablist" aria-label="Detail view">
        <button
          type="button"
          role="tab"
          :aria-selected="detailTab === 'document'"
          :class="{ active: detailTab === 'document' }"
          @click="detailTab = 'document'"
        >
          <i class="pi pi-file" />Document
        </button>
        <button
          type="button"
          role="tab"
          :aria-selected="detailTab === 'details'"
          :class="{ active: detailTab === 'details' }"
          @click="detailTab = 'details'"
        >
          <i class="pi pi-sparkles" />Details
        </button>
      </div>

      <div class="detail-body" :data-tab="detailTab">
        <div class="detail-main">
          <section v-if="sourceLoading || sourceError || (source?.type === 'html' && source.content) || isPdf" class="detail-section source-section">
            <Message v-if="sourceError" severity="warn" :closable="false">{{ sourceError }}</Message>
            <div v-if="sourceLoading" class="preview-loading compact-loading"><ProgressSpinner /><span>Loading source</span></div>
            <div v-else-if="source?.type === 'html' && source.content" class="source-frame">
              <div class="sbp-source-content" v-html="source.content" @click="handleSourceClick" />
            </div>
            <button v-else-if="isPdf" type="button" class="pdf-source-compact" @click="pdfDialogVisible = true">
              <i class="pi pi-file-pdf" /><span><strong>PDF source</strong><small>Open the document preview</small></span><i class="pi pi-angle-right" />
            </button>
          </section>
          <div v-else class="detail-section detail-source-empty">
            <i class="pi pi-file-o" />
            <span>No source content available for this circular.</span>
          </div>
        </div>

        <aside class="detail-rail" aria-label="Circular intelligence">
          <template v-if="hasIntelligence">
          <section v-if="circular.summary" class="detail-section summary-section">
            <h2>
              <button
                type="button"
                class="collapsible-heading"
                :aria-expanded="summaryExpanded"
                @click="summaryExpanded = !summaryExpanded"
              >
                <span class="section-label"><i class="pi pi-align-left section-icon" />Summary</span>
                <i :class="summaryExpanded ? 'pi pi-chevron-up' : 'pi pi-chevron-down'" />
              </button>
            </h2>
            <div
              v-show="summaryExpanded"
              class="detail-copy markdown-body summary-markdown"
              v-html="renderMarkdown(circular.summary)"
            />
          </section>

          <section
            v-if="circular.relationships.outgoing.length || circular.relationships.incoming.length"
            class="detail-section intelligence-section"
          >
            <div class="pill-group">
              <h2><i class="pi pi-sitemap section-icon" />Relationships</h2>
              <div class="relationship-groups">
                <div
                  v-for="group in relationshipGroups"
                  :key="group.key"
                  class="relationship-group"
                  :class="{ incoming: group.direction === 'incoming' }"
                >
                  <span class="relationship-group-chip">
                    {{ group.label }}<span class="relationship-group-count">{{ group.items.length }}</span>
                  </span>
                  <button
                    v-for="(item, index) in visibleItems(group)"
                    :key="`${group.key}-${index}`"
                    type="button"
                    class="intelligence-pill relationship-ref-pill"
                    :disabled="!item.id"
                    @click="openRelationship(item.id)"
                  >
                    {{ item.label }}
                  </button>
                  <button
                    v-if="group.items.length > COLLAPSE_THRESHOLD"
                    type="button"
                    class="relationship-show-more"
                    @click="toggleGroup(group.key)"
                  >
                    {{ expandedGroups.has(group.key) ? 'Show less' : `+${group.items.length - COLLAPSE_THRESHOLD} more` }}
                  </button>
                </div>
              </div>
            </div>
          </section>

          <section v-if="entityGroups.length" class="detail-section entities-section">
            <h2><i class="pi pi-percentage section-icon" />Regulatory Values</h2>
            <div class="entity-groups">
              <div v-for="group in entityGroups" :key="group.type" class="entity-group">
                <span class="entity-group-label">{{ group.label }}</span>
                <ul class="entity-list">
                  <li v-for="entity in group.items" :key="entity.id" class="entity-item">
                    <div class="entity-line">
                      <span class="entity-metric">{{ entity.metric || '—' }}</span>
                      <span class="entity-value">
                        {{ formatEntityValue(entity) }}
                        <span v-if="entity.unit && entity.unit !== '%' && !entity.value_text?.includes(entity.unit)" class="entity-unit">{{ entity.unit }}</span>
                      </span>
                    </div>
                    <div
                      v-if="entity.subject || (entity.effective_date && entity.entity_type !== 'deadline' && entity.entity_type !== 'effective_date')"
                      class="entity-sub"
                    >
                      <span v-if="entity.subject" class="entity-subject">{{ entity.subject }}</span>
                      <span
                        v-if="entity.effective_date && entity.entity_type !== 'deadline' && entity.entity_type !== 'effective_date'"
                        class="entity-effective"
                      >
                        <i class="pi pi-calendar" /> {{ formatDate(entity.effective_date) }}
                      </span>
                    </div>
                  </li>
                </ul>
              </div>
            </div>
          </section>

          <section v-if="circular.attachments.length" class="detail-section documents-section">
            <h2><i class="pi pi-paperclip section-icon" />Documents</h2>
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
          </template>

          <div v-else class="detail-rail-empty">
            <i class="pi pi-sparkles" />
            <p class="detail-rail-empty-title">No AI analysis yet</p>
            <p class="detail-rail-empty-text">
              Generate a summary, tags, relationships, and regulatory values for this circular.
            </p>
            <Button
              icon="pi pi-sparkles"
              label="Generate analysis"
              size="small"
              :loading="Boolean(activeJob)"
              @click="generate('all')"
            />
          </div>
        </aside>
      </div>
    </div>

    <PdfPreviewDialog v-model:visible="pdfDialogVisible" :title="circular?.title || 'Circular'" :url="sourceUrl" />
    <PdfPreviewDialog
      v-if="selectedAttachment"
      v-model:visible="attachmentDialogVisible"
      :title="selectedAttachment.filename"
      :document-id="selectedAttachment.id"
    />

    <Dialog
      v-if="circular"
      v-model:visible="graphVisible"
      :header="`Related — ${circular.reference || circular.title}`"
      modal
      :style="{ width: '90vw', maxWidth: '1100px', height: '75vh' }"
      :content-style="{ height: 'calc(75vh - 60px)', padding: 0 }"
      :draggable="false"
    >
      <CircularGraph :circular="circular" @navigate="navigateFromGraph" />
    </Dialog>
  </aside>
</template>

<style scoped>
.entity-groups {
  display: flex;
  flex-direction: column;
  gap: 0.85rem;
}

.entity-group-label {
  display: block;
  font-size: 0.64rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--p-text-muted-color, #6b7280);
  margin-bottom: 0.25rem;
}

.entity-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.entity-item {
  padding: 0.3rem 0;
  border-top: 1px solid var(--p-content-border-color, #e5e7eb);
}

.entity-line {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 0.4rem;
  font-size: 0.72rem;
  line-height: 1.3;
}

.entity-metric {
  font-weight: 600;
  min-width: 0;
  overflow-wrap: anywhere;
}

.entity-value {
  flex: 0 1 auto;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  text-align: right;
  overflow-wrap: anywhere;
}

.entity-unit {
  color: var(--p-text-muted-color, #6b7280);
  font-weight: 400;
  margin-left: 0.2rem;
}

.entity-sub {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0.2rem 0.5rem;
  margin-top: 0.12rem;
  font-size: 0.68rem;
  line-height: 1.3;
}

.entity-subject {
  color: var(--p-text-muted-color, #4b5563);
  overflow-wrap: anywhere;
}

.entity-effective {
  display: inline-flex;
  align-items: center;
  gap: 0.2rem;
  color: var(--p-text-muted-color, #6b7280);
  white-space: nowrap;
}
</style>
