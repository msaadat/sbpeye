<script setup lang="ts">
import { computed, nextTick, ref, shallowRef, watch } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import Message from 'primevue/message'
import ProgressSpinner from 'primevue/progressspinner'
import {
  GlobalWorkerOptions,
  getDocument,
  type PDFDocumentLoadingTask,
  type PDFDocumentProxy,
  type RenderTask,
} from 'pdfjs-dist'
import pdfWorkerUrl from 'pdfjs-dist/build/pdf.worker.mjs?url'
import { buildPdfProxyUrl, getPdfPreview, type PdfPreviewResponse } from '@/lib/api'

GlobalWorkerOptions.workerSrc = pdfWorkerUrl

const props = defineProps<{
  visible: boolean
  title?: string
  url?: string | null
}>()

const emit = defineEmits<{
  'update:visible': [value: boolean]
}>()

const canvasRef = shallowRef<HTMLCanvasElement | null>(null)
const documentRef = shallowRef<PDFDocumentProxy | null>(null)
const loadingTaskRef = shallowRef<PDFDocumentLoadingTask | null>(null)
const renderTaskRef = shallowRef<RenderTask | null>(null)
const preview = ref<PdfPreviewResponse | null>(null)
const loading = ref(false)
const rendering = ref(false)
const errorMessage = ref('')
const pageNumber = ref(1)
const pageCount = ref(0)
const scale = ref(1.25)

const dialogVisible = computed({
  get: () => props.visible,
  set: (value: boolean) => emit('update:visible', value),
})

const pdfProxyUrl = computed(() => (props.url ? buildPdfProxyUrl(props.url) : ''))

const pageLabel = computed(() => {
  if (!pageCount.value) {
    return 'PDF source'
  }

  return `Page ${pageNumber.value} of ${pageCount.value}`
})

const fallbackImageSrc = computed(() => {
  if (preview.value?.type !== 'image' || !preview.value.content) {
    return ''
  }

  return `data:image/png;base64,${preview.value.content}`
})

function resetState() {
  preview.value = null
  errorMessage.value = ''
  pageNumber.value = 1
  pageCount.value = 0
}

async function cancelRender() {
  if (!renderTaskRef.value) {
    return
  }

  const task = renderTaskRef.value
  renderTaskRef.value = null
  task.cancel()

  try {
    await task.promise
  } catch {
    // PDF.js rejects cancelled render tasks; cancellation is expected here.
  }
}

async function closeDocument() {
  await cancelRender()

  if (loadingTaskRef.value) {
    await loadingTaskRef.value.destroy()
    loadingTaskRef.value = null
  }

  documentRef.value = null
}

async function renderPage() {
  const canvas = canvasRef.value
  const document = documentRef.value
  if (!canvas || !document) {
    return
  }

  await cancelRender()
  rendering.value = true
  errorMessage.value = ''

  try {
    const page = await document.getPage(pageNumber.value)
    const viewport = page.getViewport({ scale: scale.value })
    const context = canvas.getContext('2d')

    if (!context) {
      throw new Error('Canvas rendering is unavailable in this browser.')
    }

    const pixelRatio = window.devicePixelRatio || 1
    canvas.width = Math.floor(viewport.width * pixelRatio)
    canvas.height = Math.floor(viewport.height * pixelRatio)
    canvas.style.width = `${Math.floor(viewport.width)}px`
    canvas.style.height = `${Math.floor(viewport.height)}px`

    context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0)
    context.clearRect(0, 0, viewport.width, viewport.height)

    const task = page.render({ canvas, canvasContext: context, viewport })
    renderTaskRef.value = task
    await task.promise
  } catch (error) {
    if (!(error instanceof Error) || error.name !== 'RenderingCancelledException') {
      errorMessage.value = error instanceof Error ? error.message : 'Unable to render this PDF page.'
    }
  } finally {
    renderTaskRef.value = null
    rendering.value = false
  }
}

async function loadPdf() {
  if (!props.url) {
    errorMessage.value = 'No PDF URL was provided.'
    return
  }

  await closeDocument()
  resetState()
  loading.value = true

  const previewPromise = getPdfPreview(props.url)
    .then((value) => {
      preview.value = value
      if (value.pages && !pageCount.value) {
        pageCount.value = value.pages
      }
    })
    .catch(() => undefined)

  try {
    const loadingTask = getDocument({
      url: pdfProxyUrl.value,
      withCredentials: false,
    })
    loadingTaskRef.value = loadingTask

    const document = await loadingTask.promise
    documentRef.value = document
    pageCount.value = document.numPages
    await nextTick()
    await renderPage()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'Unable to open this PDF.'
  } finally {
    await previewPromise
    loading.value = false
  }
}

function previousPage() {
  if (pageNumber.value <= 1) {
    return
  }

  pageNumber.value -= 1
  void renderPage()
}

function nextPage() {
  if (!pageCount.value || pageNumber.value >= pageCount.value) {
    return
  }

  pageNumber.value += 1
  void renderPage()
}

function zoomOut() {
  scale.value = Math.max(0.75, Number((scale.value - 0.25).toFixed(2)))
  void renderPage()
}

function zoomIn() {
  scale.value = Math.min(2.5, Number((scale.value + 0.25).toFixed(2)))
  void renderPage()
}

watch(
  () => props.visible,
  (visible) => {
    if (visible) {
      void loadPdf()
      return
    }

    void closeDocument()
  },
)

watch(
  () => props.url,
  () => {
    if (props.visible) {
      void loadPdf()
    }
  },
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
          <span>{{ pageLabel }}</span>
          <span v-if="preview?.pages && preview.pages !== pageCount">
            {{ preview.pages }} pages reported by server
          </span>
        </div>
        <div class="pdf-preview-actions">
          <Button
            icon="pi pi-chevron-left"
            text
            rounded
            :disabled="loading || rendering || pageNumber <= 1"
            aria-label="Previous page"
            @click="previousPage"
          />
          <Button
            icon="pi pi-chevron-right"
            text
            rounded
            :disabled="loading || rendering || !pageCount || pageNumber >= pageCount"
            aria-label="Next page"
            @click="nextPage"
          />
          <Button
            icon="pi pi-search-minus"
            text
            rounded
            :disabled="loading || rendering || scale <= 0.75"
            aria-label="Zoom out"
            @click="zoomOut"
          />
          <Button
            icon="pi pi-search-plus"
            text
            rounded
            :disabled="loading || rendering || scale >= 2.5"
            aria-label="Zoom in"
            @click="zoomIn"
          />
          <Button
            v-if="url"
            as="a"
            :href="url"
            target="_blank"
            rel="noreferrer"
            label="Open source"
            icon="pi pi-external-link"
            size="small"
            outlined
          />
        </div>
      </div>

      <Message v-if="errorMessage" severity="error" :closable="false">
        {{ errorMessage }}
      </Message>

      <div v-if="loading" class="preview-loading">
        <ProgressSpinner aria-label="Loading PDF preview" />
        <span>Loading PDF preview</span>
      </div>

      <div v-show="!loading && documentRef" class="pdf-canvas-shell">
        <div v-if="rendering" class="pdf-rendering-mask">
          <i class="pi pi-spin pi-spinner" />
        </div>
        <canvas ref="canvasRef" class="pdf-canvas" />
      </div>

      <template v-if="!loading && !documentRef">
        <pre v-if="preview?.type === 'text'" class="pdf-text-preview">{{ preview.content }}</pre>
        <img
          v-else-if="fallbackImageSrc"
          :src="fallbackImageSrc"
          alt="First page preview"
          class="pdf-image-preview"
        >
        <Message v-else-if="!errorMessage" severity="info" :closable="false">
          No preview content was returned for this PDF.
        </Message>
      </template>
    </div>
  </Dialog>
</template>
