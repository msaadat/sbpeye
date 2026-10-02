<script setup lang="ts">
import Tag from 'primevue/tag'
import type { CircularSummary } from '@/lib/api'
import { circularStatusTone } from '@/lib/circularStatus'

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
</script>

<template>
  <span class="result-content">
    <strong>{{ circular.title }}</strong>
    <span class="result-topline">
      <span class="result-reference">{{ circular.reference || 'No reference' }} · {{ formatDate(circular.date) }}</span>
      <span
        v-if="circular.status && circular.status !== 'active'"
        class="status-chip"
        :class="`status-${circularStatusTone(circular.status)}`"
      >
        <span class="status-dot" />{{ circular.status }}
      </span>
    </span>
    <span v-if="showSnippet && circular.snippet" class="result-snippet" v-html="circular.snippet" />
    <span v-else-if="showSnippet && circular.summary" class="result-snippet">{{ circular.summary }}</span>
    <span v-if="circular.source_page" class="result-reference-location">
      {{ circular.source_ref || `Page ${circular.source_page}` }}
    </span>
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
