<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import Message from 'primevue/message'
import ProgressSpinner from 'primevue/progressspinner'
import { buildDocumentContentUrl, buildPdfProxyUrl } from '@/lib/api'

const props = defineProps<{
  visible: boolean
  title?: string
  url?: string | null
  documentId?: string | null
}>()

const emit = defineEmits<{
  'update:visible': [value: boolean]
}>()

const loading = ref(false)
const frameKey = ref(0)

const dialogVisible = computed({
  get: () => props.visible,
  set: (value: boolean) => emit('update:visible', value),
})

const pdfProxyUrl = computed(() => props.documentId
  ? buildDocumentContentUrl(props.documentId)
  : props.url?.startsWith('/api/') ? props.url
  : props.url ? buildPdfProxyUrl(props.url) : '')

const canShowPdf = computed(() => Boolean(pdfProxyUrl.value))

const openPdfUrl = computed(() => pdfProxyUrl.value || '#')

function refreshViewer() {
  frameKey.value += 1
  loading.value = Boolean(pdfProxyUrl.value)
}

function handleFrameLoad() {
  loading.value = false
}

watch(
  () => [props.visible, pdfProxyUrl.value] as const,
  ([visible, source]) => {
    if (visible && source) {
      refreshViewer()
    } else {
      loading.value = false
    }
  },
  { immediate: true },
)
</script>

<template>
  <Dialog
    v-model:visible="dialogVisible"
    modal
    class="pdf-preview-dialog"
    :header="title || 'PDF preview'"
  >
    <div class="pdf-preview">
      <div class="pdf-preview-toolbar">
        <div class="pdf-preview-meta">
          <span>PDF viewer</span>
        </div>
        <div class="pdf-preview-actions">
          <Button
            icon="pi pi-refresh"
            text
            rounded
            :disabled="!canShowPdf"
            aria-label="Reload PDF"
            title="Reload PDF"
            @click="refreshViewer"
          />
          <Button
            v-if="canShowPdf"
            as="a"
            :href="openPdfUrl"
            target="_blank"
            rel="noreferrer"
            label="Open in browser"
            icon="pi pi-external-link"
            size="small"
            outlined
          />
        </div>
      </div>

      <Message v-if="!canShowPdf" severity="error" :closable="false">
        No PDF URL was provided.
      </Message>

      <div v-if="loading && canShowPdf" class="preview-loading">
        <ProgressSpinner aria-label="Loading PDF preview" />
        <span>Loading PDF preview</span>
      </div>

      <iframe
        v-if="canShowPdf"
        :key="frameKey"
        class="pdf-native-viewer"
        :src="pdfProxyUrl"
        :title="title || 'PDF preview'"
        @load="handleFrameLoad"
      />
    </div>
  </Dialog>
</template>
