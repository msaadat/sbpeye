<script setup lang="ts">
import Tag from 'primevue/tag'
import type { CircularSummary } from '@/lib/api'
import { circularStanding, standingLabel } from '@/lib/circularStatus'
import { displayTitle } from '@/lib/displayTitle'

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
    <strong>{{ displayTitle(circular.title) }}</strong>
    <span class="result-topline">
      <span class="result-reference">{{ circular.reference || 'No reference' }} · {{ formatDate(circular.date) }}</span>
      <!-- No chip for "in force" in a list: it is the default, and a green chip on every
           row of a 300px rail pushed the reference out. The detail header always shows it. -->
      <span
        v-if="circular.status && circularStanding(circular.status) !== 'in-force'"
        class="status-chip"
        :class="`status-${circularStanding(circular.status)}`"
      >
        <span class="status-dot" />{{ standingLabel(circular.status) }}
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
