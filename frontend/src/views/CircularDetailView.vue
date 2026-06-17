<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Divider from 'primevue/divider'
import Message from 'primevue/message'
import ProgressSpinner from 'primevue/progressspinner'
import Tag from 'primevue/tag'
import StateBlock from '@/components/StateBlock.vue'
import {
  getCircularDetail,
  getCircularSource,
  type CircularDetail,
  type CircularRelationship,
  type CircularRelationshipTarget,
  type CircularSourceContent,
} from '@/lib/api'

const PdfPreviewDialog = defineAsyncComponent(() => import('@/components/PdfPreviewDialog.vue'))

const props = defineProps<{
  id: string
}>()

const router = useRouter()

const circular = ref<CircularDetail | null>(null)
const source = ref<CircularSourceContent | null>(null)
const loading = ref(false)
const sourceLoading = ref(false)
const errorMessage = ref('')
const sourceError = ref('')
const pdfDialogVisible = ref(false)

const isPdf = computed(() => {
  const url = source.value?.url || circular.value?.url
  return source.value?.type === 'pdf' || Boolean(url?.toLowerCase().split('?', 1)[0].endsWith('.pdf'))
})

const sourceUrl = computed(() => source.value?.url || circular.value?.url || '')

const pageTitle = computed(() => circular.value?.title || `Circular ${props.id}`)

function formatDate(value?: string | null): string {
  if (!value) {
    return 'Not dated'
  }

  return new Intl.DateTimeFormat(undefined, {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
  }).format(new Date(value))
}

function statusSeverity(status?: string | null): 'success' | 'info' | 'warn' | 'danger' | 'secondary' | 'contrast' {
  const normalized = (status || '').toLowerCase()

  if (normalized.includes('active') || normalized.includes('indexed')) {
    return 'success'
  }

  if (normalized.includes('superseded') || normalized.includes('replaced')) {
    return 'warn'
  }

  if (normalized.includes('withdrawn') || normalized.includes('cancel')) {
    return 'danger'
  }

  return status ? 'info' : 'secondary'
}

function relationshipLabel(value?: string | null): string {
  if (!value) {
    return 'Related'
  }

  return value
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/\b\w/g, (match) => match.toUpperCase())
}

function relationTarget(relation: CircularRelationship, direction: 'outgoing' | 'incoming'): CircularRelationshipTarget | null {
  if (direction === 'incoming') {
    return relation.source || null
  }

  return relation.target || null
}

function unresolvedReference(relation: CircularRelationship, direction: 'outgoing' | 'incoming'): string {
  if (direction === 'incoming') {
    return relation.source_id || 'Unresolved source circular'
  }

  return relation.target_reference || relation.target_id || 'Unresolved target circular'
}

function openPdfPreview() {
  if (!sourceUrl.value || !isPdf.value) {
    return
  }

  pdfDialogVisible.value = true
}

function handoffToChat() {
  void router.push({
    path: '/chat',
    query: {
      circular_ids: props.id,
    },
  })
}

async function loadCircular() {
  loading.value = true
  sourceLoading.value = true
  errorMessage.value = ''
  sourceError.value = ''
  circular.value = null
  source.value = null

  try {
    const detail = await getCircularDetail(props.id)
    circular.value = detail
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
    if (source.value.error) {
      sourceError.value = source.value.error
    }
  } catch (error) {
    sourceError.value = error instanceof Error ? error.message : 'Unable to load circular source.'
  } finally {
    sourceLoading.value = false
  }
}

onMounted(() => {
  void loadCircular()
})

watch(
  () => props.id,
  () => {
    void loadCircular()
  },
)
</script>

<template>
  <section class="view-stack">
    <div class="page-heading detail-heading">
      <div>
        <p>Circular detail</p>
        <h1>{{ pageTitle }}</h1>
      </div>
      <div class="button-row">
        <Button
          v-if="isPdf"
          label="Preview PDF"
          icon="pi pi-file-pdf"
          severity="danger"
          :disabled="!sourceUrl"
          @click="openPdfPreview"
        />
        <Button
          v-if="circular?.url"
          as="a"
          :href="circular.url"
          target="_blank"
          rel="noreferrer"
          label="Open source"
          icon="pi pi-external-link"
          severity="secondary"
          outlined
        />
        <Button
          label="Open in chat"
          icon="pi pi-comments"
          severity="contrast"
          :disabled="!circular"
          @click="handoffToChat"
        />
      </div>
    </div>

    <Message v-if="errorMessage" severity="error" :closable="false">
      {{ errorMessage }}
    </Message>

    <div v-if="loading" class="preview-loading">
      <ProgressSpinner aria-label="Loading circular detail" />
      <span>Loading circular detail</span>
    </div>

    <template v-else-if="circular">
      <Card>
        <template #content>
          <div class="detail-grid">
            <div>
              <span>Reference</span>
              <strong>{{ circular.reference || 'No reference' }}</strong>
            </div>
            <div>
              <span>Department</span>
              <strong>{{ circular.department || 'Unassigned' }}</strong>
            </div>
            <div>
              <span>Date</span>
              <strong>{{ formatDate(circular.date) }}</strong>
            </div>
            <div>
              <span>Status</span>
              <Tag :value="circular.status" :severity="statusSeverity(circular.status)" />
            </div>
            <div>
              <span>Circular ID</span>
              <strong>{{ circular.id }}</strong>
            </div>
            <div>
              <span>Source type</span>
              <strong>{{ isPdf ? 'PDF' : source?.type === 'html' ? 'HTML' : 'Source' }}</strong>
            </div>
          </div>
        </template>
      </Card>

      <div class="detail-layout">
        <div class="detail-main">
          <Card>
            <template #title>Source content</template>
            <template #content>
              <Message v-if="sourceError" severity="warn" :closable="false">
                {{ sourceError }}
              </Message>

              <div v-if="sourceLoading" class="preview-loading compact-loading">
                <ProgressSpinner aria-label="Loading circular source" />
                <span>Loading source content</span>
              </div>

              <div v-else-if="source?.type === 'html' && source.content" class="source-frame">
                <div class="sbp-source-content" v-html="source.content" />
              </div>

              <div v-else-if="isPdf" class="pdf-source-placeholder">
                <i class="pi pi-file-pdf" />
                <div>
                  <strong>PDF circular source</strong>
                  <p>Open the PDF preview to read the original SBP document inside the workspace.</p>
                </div>
                <Button
                  label="Preview PDF"
                  icon="pi pi-file-pdf"
                  severity="danger"
                  :disabled="!sourceUrl"
                  @click="openPdfPreview"
                />
              </div>

              <StateBlock
                v-else-if="!sourceError"
                title="No source content available"
                message="This circular does not currently have source content available through the API."
                icon="pi-file"
              />
            </template>
          </Card>
        </div>

        <aside class="detail-side">
          <Card>
            <template #title>Summary</template>
            <template #content>
              <p v-if="circular.summary" class="detail-copy">{{ circular.summary }}</p>
              <p v-else class="muted-text">No summary has been generated for this circular.</p>
            </template>
          </Card>

          <Card>
            <template #title>Compliance checklist</template>
            <template #content>
              <ul v-if="circular.compliance_checklist?.length" class="checklist">
                <li v-for="item in circular.compliance_checklist" :key="item">
                  <i class="pi pi-check-circle" />
                  <span>{{ item }}</span>
                </li>
              </ul>
              <p v-else class="muted-text">No checklist items are available.</p>
            </template>
          </Card>

          <Card>
            <template #title>Tags</template>
            <template #content>
              <div v-if="circular.tags?.length" class="tag-list">
                <Tag
                  v-for="item in circular.tags"
                  :key="item"
                  :value="item"
                  severity="secondary"
                />
              </div>
              <p v-else class="muted-text">No tags are assigned.</p>
            </template>
          </Card>

          <Card>
            <template #title>Relationships</template>
            <template #content>
              <div class="relationship-section">
                <h2>Outgoing</h2>
                <div v-if="circular.relationships.outgoing.length" class="relationship-list">
                  <div
                    v-for="relation in circular.relationships.outgoing"
                    :key="`${relation.type}-${relation.target_id || relation.target_reference}`"
                    class="relationship-item"
                  >
                    <Tag :value="relationshipLabel(relation.type)" severity="info" />
                    <RouterLink
                      v-if="relationTarget(relation, 'outgoing')"
                      :to="`/circulars/${relationTarget(relation, 'outgoing')?.id}`"
                    >
                      {{ relationTarget(relation, 'outgoing')?.reference || relationTarget(relation, 'outgoing')?.title }}
                    </RouterLink>
                    <span v-else>{{ unresolvedReference(relation, 'outgoing') }}</span>
                    <small v-if="relation.confidence !== null && relation.confidence !== undefined">
                      Confidence {{ Math.round(relation.confidence * 100) }}%
                    </small>
                  </div>
                </div>
                <p v-else class="muted-text">No outgoing relationships.</p>
              </div>

              <Divider />

              <div class="relationship-section">
                <h2>Incoming</h2>
                <div v-if="circular.relationships.incoming.length" class="relationship-list">
                  <div
                    v-for="relation in circular.relationships.incoming"
                    :key="`${relation.type}-${relation.source_id || relation.target_reference}`"
                    class="relationship-item"
                  >
                    <Tag :value="relationshipLabel(relation.type)" severity="warn" />
                    <RouterLink
                      v-if="relationTarget(relation, 'incoming')"
                      :to="`/circulars/${relationTarget(relation, 'incoming')?.id}`"
                    >
                      {{ relationTarget(relation, 'incoming')?.reference || relationTarget(relation, 'incoming')?.title }}
                    </RouterLink>
                    <span v-else>{{ unresolvedReference(relation, 'incoming') }}</span>
                    <small v-if="relation.confidence !== null && relation.confidence !== undefined">
                      Confidence {{ Math.round(relation.confidence * 100) }}%
                    </small>
                  </div>
                </div>
                <p v-else class="muted-text">No incoming relationships.</p>
              </div>
            </template>
          </Card>
        </aside>
      </div>
    </template>

    <PdfPreviewDialog
      v-model:visible="pdfDialogVisible"
      :title="pageTitle"
      :url="sourceUrl"
    />
  </section>
</template>
