<script setup lang="ts">
/**
 * The collapsible summary block in a detail rail.
 *
 * Collapsed by default: a rail is scanned for relationships and values, and an expanded
 * five-sentence paragraph pushes those below the fold on first open.
 */
import { computed, ref } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

const props = defineProps<{ summary: string; label?: string }>()

const expanded = ref(false)

// Called from the template this re-parsed on every re-render of the parent rail rather
// than when the summary changed, and `v-show` meant it ran for a block that is collapsed
// — so invisible — on arrival. A computed behind `v-if` parses once, on first open.
const rendered = computed(() => DOMPurify.sanitize(marked.parse(props.summary) as string, {
  USE_PROFILES: { html: true },
}))
</script>

<template>
  <section class="detail-section summary-section">
    <h2>
      <button
        type="button"
        class="collapsible-heading"
        :aria-expanded="expanded"
        @click="expanded = !expanded"
      >
        <span class="section-label">
          <i class="pi pi-align-left section-icon" />{{ props.label || 'Summary' }}
        </span>
        <i :class="expanded ? 'pi pi-chevron-up' : 'pi pi-chevron-down'" />
      </button>
    </h2>
    <div
      v-if="expanded"
      class="detail-copy markdown-body summary-markdown"
      v-html="rendered"
    />
  </section>
</template>
