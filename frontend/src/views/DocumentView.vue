<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import Button from 'primevue/button'
import Message from 'primevue/message'
import ProgressSpinner from 'primevue/progressspinner'
import { buildDocumentContentUrl, resolveDocument, type ResolvedDocument } from '@/lib/api'

const PdfPreviewDialog = defineAsyncComponent(() => import('@/components/PdfPreviewDialog.vue'))
const route = useRoute()
const document = ref<ResolvedDocument | null>(null)
const loading = ref(true)
const refreshing = ref(false)
const errorMessage = ref('')
const originalUrl = ref('')
const pdfVisible = ref(false)

const isPdf = computed(() => document.value?.file_type?.toLowerCase() === 'pdf')

function queryValue(name: string): string {
  const value = route.query[name]
  return Array.isArray(value) ? String(value[0] || '') : typeof value === 'string' ? value : ''
}

async function load(refresh = false) {
  loading.value = !refresh
  refreshing.value = refresh
  errorMessage.value = ''
  originalUrl.value = queryValue('url')
  try {
    document.value = await resolveDocument({
      id: queryValue('id') || undefined,
      url: originalUrl.value || undefined,
      circular_id: queryValue('circular_id') || undefined,
    }, refresh)
    originalUrl.value = document.value.original_url
    if (isPdf.value) pdfVisible.value = true
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'Unable to cache this document.'
  } finally {
    loading.value = false
    refreshing.value = false
  }
}

onMounted(() => load())
</script>

<template>
  <main class="document-route-page">
    <div v-if="loading" class="preview-loading"><ProgressSpinner /><span>Opening document from local cache</span></div>
    <template v-else-if="document">
      <section class="document-route-card">
        <i class="pi pi-file document-route-icon" />
        <div><small>{{ document.file_type?.toUpperCase() || 'Document' }}</small><h1>{{ document.filename }}</h1><p>Served from SBPEye's local cache.</p></div>
        <div class="route-error-actions">
          <Button v-if="isPdf" label="Open viewer" icon="pi pi-file-pdf" @click="pdfVisible = true" />
          <Button v-else as="a" :href="buildDocumentContentUrl(document.id)" label="Open local file" icon="pi pi-download" />
          <Button label="Refresh from SBP" icon="pi pi-refresh" outlined :loading="refreshing" @click="load(true)" />
        </div>
      </section>
    </template>
    <template v-else>
      <Message severity="error" :closable="false">{{ errorMessage }}</Message>
      <div class="route-error-actions">
        <Button label="Retry" icon="pi pi-refresh" @click="load()" />
        <Button v-if="originalUrl" as="a" :href="originalUrl" target="_blank" rel="noreferrer" label="Open original on SBP" outlined />
      </div>
    </template>
    <PdfPreviewDialog v-if="document && isPdf" v-model:visible="pdfVisible" :title="document.filename" :document-id="document.id" />
  </main>
</template>
