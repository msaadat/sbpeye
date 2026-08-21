<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import Message from 'primevue/message'
import Popover from 'primevue/popover'
import ProgressSpinner from 'primevue/progressspinner'
import {
  buildDocumentContentUrl,
  getCircularDetail,
  getCircularSource,
  downloadChecklistExcel,
  refreshCircular as refreshCircularSource,
  startCircularGeneration,
  type CircularDetail,
  type CircularAttachment,
  type GenerationAction,
  type GenerationFeature,
  type CircularRelationship,
  type CircularRelationshipTarget,
  type CircularSourceContent,
  type LawSummary,
} from '@/lib/api'
import { useResizablePane } from '@/lib/useResizablePane'
import { useAiGeneration } from '@/lib/useAiGeneration'
import { ADMIN_ONLY_EMPTY_HINT, adminOnlyHint, adminOnlyLabel } from '@/lib/adminOnly'
import { useCurrentUser } from '@/lib/useCurrentUser'
import RelationshipGroups, {
  type RelationGroup,
} from '@/components/RelationshipGroups.vue'
import RegulatoryValueList from '@/components/RegulatoryValueList.vue'
import SummarySection from '@/components/SummarySection.vue'

const PdfPreviewDialog = defineAsyncComponent(() => import('@/components/PdfPreviewDialog.vue'))
const CircularGraph = defineAsyncComponent(() => import('@/components/CircularGraph.vue'))
const ConsolidatedView = defineAsyncComponent(() => import('@/components/ConsolidatedView.vue'))

type PreviewAttachment = Pick<CircularAttachment, 'id' | 'filename' | 'file_type'>

const props = defineProps<{ id: string, isPinned: boolean, pinPending: boolean }>()
const emit = defineEmits<{ close: [], 'toggle-pin': [] }>()
const router = useRouter()
const toast = useToast()
// Generation and re-fetching write the shared corpus, so the server allows them only to
// an admin. The controls stay on the page for everyone and say why they are inert.
const { isAdmin, load: loadCurrentUser } = useCurrentUser()

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
const generationPopover = ref<InstanceType<typeof Popover> | null>(null)
const graphVisible = ref(false)
const consolidatedVisible = ref(false)
const graphFocusLabel = ref<string | null>(null)
watch(graphVisible, visible => { if (visible) graphFocusLabel.value = null })
const graphHeader = computed(
  () => `Related — ${graphFocusLabel.value || circular.value?.reference || circular.value?.title || ''}`,
)
const detailTab = ref<'document' | 'details'>('document')
const detailRail = useResizablePane('sbp:detailRailWidth', 336, 240, 480, { reverse: true })

const generationFeatures: Array<{ feature: GenerationFeature; label: string; icon: string }> = [
  { feature: 'summary', label: 'Summary', icon: 'pi pi-align-left' },
  { feature: 'tags', label: 'Tags', icon: 'pi pi-tags' },
  { feature: 'checklist', label: 'Checklist', icon: 'pi pi-list-check' },
  { feature: 'relationships', label: 'Relationships', icon: 'pi pi-share-alt' },
  { feature: 'entities', label: 'Regulatory Values', icon: 'pi pi-percentage' },
]

const entityCount = computed(() => circular.value?.entities?.length ?? 0)

const sourceUrl = computed(() => source.value?.url || circular.value?.url || '')
const sourceWebsiteUrl = computed(() => source.value?.original_url || circular.value?.url || source.value?.url || '')
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

const INCOMING_LABELS: Record<string, string> = {
  supersedes: 'Superseded by',
  amends: 'Amended by',
  cancels: 'Cancelled by',
  clarifies: 'Clarified by',
  adds_to: 'Added to by',
}

const TYPE_ORDER = ['supersedes', 'amends', 'cancels', 'clarifies', 'adds_to']
const CONSOLIDATION_RELATION_TYPES = new Set(['amends', 'adds_to'])

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
  const rank = (type?: string) => {
    const index = TYPE_ORDER.indexOf(type || '')
    return index === -1 ? TYPE_ORDER.length : index
  }
  return groups.sort((a, b) => {
    if (a.direction !== b.direction) return a.direction === 'outgoing' ? -1 : 1
    return rank(a.type) - rank(b.type)
  })
})

function openRelationship(id?: string | null) {
  if (!id) return
  void router.push({ path: `/circulars/${id}`, query: router.currentRoute.value.query })
}

/** Into the laws corpus, not the circulars list — a different destination entirely. */
function openRegulation(id: string) {
  void router.push(`/laws/${encodeURIComponent(id)}`)
}

/** Some parts are titled with their own label ("NBFCs"); prefixing it reads as a stutter. */
function regulationPartLabel(document: LawSummary): string {
  const label = document.part_label
  return !label || label === document.display_title ? '' : label
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

/**
 * What "Generate All" covers on the backend. The checklist is the priciest feature — one
 * model call per chunk — so `all` skips it and it is generated on request instead.
 */
const BULK_FEATURES: GenerationFeature[] = generationFeatures
  .map(({ feature }) => feature)
  .filter((feature) => feature !== 'checklist')

const allGenerated = computed(() => BULK_FEATURES.every((feature) => hasGenerated(feature)))
const hasRelationships = computed(() =>
  Boolean(circular.value?.relationships.outgoing.length || circular.value?.relationships.incoming.length),
)
const hasConsolidationChain = computed(() => {
  const relationships = circular.value?.relationships
  if (!relationships) return false
  return (
    relationships.outgoing.some((relation) => CONSOLIDATION_RELATION_TYPES.has(relation.type) && relation.target?.id) ||
    relationships.incoming.some((relation) => CONSOLIDATION_RELATION_TYPES.has(relation.type) && relation.source?.id)
  )
})
const hasIntelligence = computed(() =>
  Boolean(
    circular.value?.summary ||
      hasRelationships.value ||
      entityCount.value ||
      // Regulations cited are deterministic — URL scans and SBP's own listing, no model
      // involved — so a circular with no AI pass must still show them rather than fall
      // through to "No AI analysis yet". Attachments are in here for the same reason.
      circular.value?.regulations.length ||
      circular.value?.attachments.length,
  ),
)

function navigateFromGraph(id: string) {
  graphVisible.value = false
  void router.push({ path: `/circulars/${id}`, query: router.currentRoute.value.query })
}

function navigateFromConsolidated(id: string) {
  consolidatedVisible.value = false
  void router.push({ path: `/circulars/${id}`, query: router.currentRoute.value.query })
}

async function refreshCircular() {
  circular.value = await getCircularDetail(props.id)
}

const { activeJob, generate: startGeneration, stop: stopPolling } = useAiGeneration({
  start: (feature) => startCircularGeneration(props.id, feature as GenerationAction),
  refresh: refreshCircular,
  subject: 'circular',
})

function generate(feature: GenerationAction) {
  generationPopover.value?.hide()
  void startGeneration(feature)
}

async function loadCircular() {
  stopPolling()
  activeJob.value = null
  consolidatedVisible.value = false
  detailTab.value = 'document'
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

onMounted(() => {
  // Deduplicated at the composable, so this costs nothing when the sidebar already
  // loaded it — but a deep link straight to a circular must not race it.
  void loadCurrentUser()
  void loadCircular()
})
watch(() => props.id, loadCircular)
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
            <span
              class="admin-gate"
              :title="isAdmin
                ? (activeJob ? `Generating ${activeJob.feature}` : 'Generate AI analysis')
                : adminOnlyHint('AI analysis')"
            >
              <Button
                icon="pi pi-sparkles"
                text
                rounded
                severity="help"
                :loading="Boolean(activeJob)"
                :disabled="!isAdmin"
                :aria-label="isAdmin ? 'Generate AI analysis' : adminOnlyLabel('Generate AI analysis')"
                @click="generationPopover?.toggle($event)"
              />
            </span>
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
            <span class="admin-gate" :title="isAdmin ? 'Refresh local copy from SBP' : adminOnlyHint('Refreshing from SBP')">
              <Button
                icon="pi pi-refresh"
                text
                rounded
                severity="secondary"
                :loading="refreshingSource"
                :disabled="!isAdmin"
                :aria-label="isAdmin ? 'Refresh from SBP' : adminOnlyLabel('Refresh from SBP')"
                @click="refreshFromSbp"
              />
            </span>
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
            <Button
              v-if="hasConsolidationChain"
              icon="pi pi-history"
              text
              rounded
              severity="warn"
              aria-label="View consolidated chain"
              title="Consolidated view of the amendment and addendum chain"
              @click="consolidatedVisible = true"
            />
            <span class="detail-actions-sep" aria-hidden="true" />
            <Button icon="pi pi-comments" text rounded severity="contrast" aria-label="Open in chat" title="Open in chat" @click="handoffToChat" />
          </div>
        </div>
        <div v-if="activeJob" class="generation-progress" role="status">
          <i class="pi pi-sparkles" />
          Generating {{ activeJob.feature === 'all' ? 'all AI analysis' : activeJob.feature }} in the background
          <span v-if="activeJob.progress_total">
            <!-- Checklist and entity progress both count LLM calls, not units of text:
                 blocks are packed into as few calls as the context window allows. -->
            ({{ activeJob.progress_completed }}/{{ activeJob.progress_total }}
            {{ activeJob.feature === 'consolidation' ? 'circulars' : 'steps' }})
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
          <template v-if="hasConsolidationChain">
            <div class="generation-menu-divider" />
            <Button
              icon="pi pi-history"
              label="Generate Consolidated View"
              text
              size="small"
              :disabled="Boolean(activeJob)"
              @click="generate('consolidation')"
            />
          </template>
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

      <div class="detail-body" :data-tab="detailTab" :style="{ '--detail-rail-width': `${detailRail.size.value}px` }">
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

        <div
          class="pane-resizer detail-resizer"
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize intelligence panel"
          :class="{ resizing: detailRail.resizing.value }"
          @pointerdown="detailRail.startDrag"
          @dblclick="detailRail.resetToDefault"
        />

        <aside class="detail-rail" aria-label="Circular intelligence">
          <template v-if="hasIntelligence">
          <SummarySection v-if="circular.summary" :summary="circular.summary" />

          <section
            v-if="circular.relationships.outgoing.length || circular.relationships.incoming.length"
            class="detail-section intelligence-section"
          >
            <div class="pill-group">
              <h2><i class="pi pi-sitemap section-icon" />Relationships</h2>
              <RelationshipGroups :groups="relationshipGroups" @select="openRelationship" />
            </div>
          </section>

          <!-- The reverse of a law's "cited by": circular first, regulation second, which
               is the direction people actually read. -->
          <section v-if="circular.regulations.length" class="detail-section regulations-section">
            <h2><i class="pi pi-book section-icon" />Regulations cited</h2>
            <ul class="regulation-list">
              <li v-for="link in circular.regulations" :key="link.document.id">
                <button
                  type="button"
                  class="regulation-item"
                  @click="openRegulation(link.document.id)"
                >
                  <!-- A part never appears without its container. -->
                  <span v-if="link.document.parent_title" class="regulation-crumb">
                    {{ link.document.parent_title }}
                  </span>
                  <span class="regulation-title">
                    <span v-if="regulationPartLabel(link.document)" class="regulation-part">
                      {{ regulationPartLabel(link.document) }}
                    </span>
                    {{ link.document.display_title }}
                  </span>
                  <span class="regulation-type">{{ link.document.doc_type || 'document' }}</span>
                </button>
              </li>
            </ul>
          </section>

          <RegulatoryValueList :entities="circular.entities ?? []" />

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
              A summary, tags, relationships, and regulatory values can be generated for
              this circular.
              <template v-if="!isAdmin">{{ ADMIN_ONLY_EMPTY_HINT }}</template>
            </p>
            <span class="admin-gate" :title="isAdmin ? '' : adminOnlyHint('AI analysis')">
              <Button
                icon="pi pi-sparkles"
                label="Generate analysis"
                size="small"
                :loading="Boolean(activeJob)"
                :disabled="!isAdmin"
                @click="generate('all')"
              />
            </span>
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
      :header="graphHeader"
      modal
      :style="{ width: '90vw', maxWidth: '1100px', height: '75vh' }"
      :content-style="{ height: 'calc(75vh - 60px)', padding: 0 }"
      :draggable="false"
    >
      <CircularGraph :circular="circular" @navigate="navigateFromGraph" @focuschange="graphFocusLabel = $event" />
    </Dialog>

    <Dialog
      v-if="circular"
      v-model:visible="consolidatedVisible"
      :header="`Consolidated view — ${circular.reference || circular.title}`"
      modal
      :style="{ width: '90vw', maxWidth: '900px', height: '80vh' }"
      :content-style="{ height: 'calc(80vh - 60px)', padding: 0 }"
      :draggable="false"
    >
      <ConsolidatedView :circular-id="circular.id" @navigate="navigateFromConsolidated" />
    </Dialog>
  </aside>
</template>

<style scoped>

.regulation-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.regulation-list li + li {
  border-top: 1px solid var(--sbp-border);
}

.regulation-item {
  display: block;
  width: 100%;
  padding: 0.35rem 0.3rem;
  border: 0;
  border-radius: var(--sbp-radius-sm);
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.regulation-item:hover {
  background: color-mix(in srgb, var(--sbp-green) 8%, transparent);
}

.regulation-item:hover .regulation-title {
  color: var(--sbp-green-text);
}

.regulation-crumb {
  display: block;
  font-size: var(--sbp-fs-eyebrow);
  line-height: 1.3;
  color: var(--sbp-green-text);
}

.regulation-title {
  display: block;
  font-size: var(--sbp-fs-meta);
  line-height: 1.35;
  font-weight: 600;
  overflow-wrap: anywhere;
}

.regulation-part {
  font-weight: 500;
  color: var(--sbp-muted);
  font-variant-numeric: tabular-nums;
}

.regulation-type {
  display: block;
  margin-top: 0.1rem;
  font-size: var(--sbp-fs-eyebrow);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--sbp-muted);
}

</style>
