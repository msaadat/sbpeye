<script setup lang="ts">
import Tag from 'primevue/tag'
import type { CircularSummary } from '@/lib/api'

withDefaults(defineProps<{
  circular: CircularSummary
  showSnippet?: boolean
  maxTags?: number
}>(), {
  showSnippet: true,
  maxTags: 3,
})

function formatDate(value?: string | null): string {
  if (!value) return 'Not dated'
  return new Intl.DateTimeFormat(undefined, {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
  }).format(new Date(value))
}

function statusSeverity(status: string): 'success' | 'info' | 'warn' | 'danger' {
  const value = status.toLowerCase()
  if (value.includes('active') || value.includes('indexed')) return 'success'
  if (value.includes('superseded') || value.includes('replaced')) return 'warn'
  if (value.includes('withdrawn') || value.includes('cancel')) return 'danger'
  return 'info'
}
</script>

<template>
  <span class="result-content">
    <strong>{{ circular.title }}</strong>
    <span class="result-topline">
      <code>{{ circular.reference || 'No reference' }} · {{ formatDate(circular.date) }}</code>
      <Tag
        v-if="circular.status && circular.status !== 'active'"
        :value="circular.status"
        :severity="statusSeverity(circular.status)"
      />
    </span>
    <span v-if="showSnippet && circular.snippet" class="result-snippet" v-html="circular.snippet" />
    <span v-else-if="showSnippet && circular.summary" class="result-snippet">{{ circular.summary }}</span>
    <span v-if="circular.tags?.length" class="result-tags">
      <Tag
        v-for="item in circular.tags.slice(0, maxTags)"
        :key="item"
        :value="item"
        severity="secondary"
      />
    </span>
  </span>
</template>
