<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Button from 'primevue/button'
import Message from 'primevue/message'
import ProgressSpinner from 'primevue/progressspinner'
import { openCircularByUrl } from '@/lib/api'

const route = useRoute()
const router = useRouter()
const errorMessage = ref('')
const originalUrl = ref('')

onMounted(async () => {
  originalUrl.value = typeof route.query.url === 'string' ? route.query.url : ''
  if (!originalUrl.value) {
    errorMessage.value = 'No circular URL was provided.'
    return
  }
  try {
    const circular = await openCircularByUrl(originalUrl.value)
    await router.replace(`/circulars/${circular.id}`)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'Unable to cache this circular.'
  }
})
</script>

<template>
  <main class="route-loading-page">
    <div v-if="!errorMessage" class="preview-loading"><ProgressSpinner /><span>Opening circular from local cache</span></div>
    <Message v-else severity="error" :closable="false">{{ errorMessage }}</Message>
    <div v-if="errorMessage" class="route-error-actions">
      <Button label="Retry" icon="pi pi-refresh" @click="$router.go(0)" />
      <Button v-if="originalUrl" as="a" :href="originalUrl" target="_blank" rel="noreferrer" label="Open original on SBP" outlined />
    </div>
  </main>
</template>
