<script setup lang="ts">
/**
 * How much of a corpus carries one AI-generated output.
 *
 * The bar is the point: a table of "6 / 3,653" reads as a number, while a bar three
 * pixels wide reads as a gap. Coverage on this deployment is genuinely lopsided —
 * tags are near-complete, summaries and checklists are barely started — and the
 * console exists partly to make that visible at a glance.
 */
import { computed } from 'vue'

import { humanize, percent } from '@/views/admin/adminFormat'

const props = defineProps<{
  feature: string
  generated: number
  total: number
}>()

const ratio = computed(() => percent(props.generated, props.total))
// A run that has barely started still gets a visible sliver, so "4 of 3,653" is
// distinguishable from "none at all" without reading the numbers.
const width = computed(() => (props.generated > 0 ? Math.max(ratio.value, 1) : 0))
const tone = computed(() => {
  if (!props.total || props.generated === 0) return 'none'
  if (ratio.value >= 95) return 'full'
  if (ratio.value >= 40) return 'partial'
  return 'sparse'
})
</script>

<template>
  <div class="coverage-row">
    <span class="coverage-feature">{{ humanize(feature) }}</span>
    <div class="coverage-track" role="img" :aria-label="`${feature}: ${generated} of ${total}`">
      <div class="coverage-fill" :class="`is-${tone}`" :style="{ width: `${width}%` }" />
    </div>
    <span class="coverage-count">
      {{ generated.toLocaleString() }} / {{ total.toLocaleString() }}
      <span class="coverage-percent">{{ total ? `${ratio}%` : '—' }}</span>
    </span>
  </div>
</template>

<style scoped>
.coverage-row {
  display: grid;
  grid-template-columns: 8rem 1fr auto;
  align-items: center;
  gap: 0.75rem;
  font-size: var(--sbp-fs-sm);
}

.coverage-feature {
  color: var(--sbp-text);
}

.coverage-track {
  height: 0.5rem;
  border-radius: var(--sbp-radius-pill);
  background: var(--sbp-subtle);
  overflow: hidden;
}

.coverage-fill {
  height: 100%;
  border-radius: var(--sbp-radius-pill);
  transition: width 0.25s var(--sbp-ease);
}

.coverage-fill.is-full {
  background: var(--sbp-success);
}

.coverage-fill.is-partial {
  background: var(--sbp-green);
}

.coverage-fill.is-sparse {
  background: var(--sbp-warning);
}

.coverage-count {
  color: var(--sbp-muted);
  font-size: var(--sbp-fs-meta);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.coverage-percent {
  display: inline-block;
  min-width: 2.5rem;
  text-align: right;
  color: var(--sbp-text);
}

@media (max-width: 40rem) {
  .coverage-row {
    grid-template-columns: 6rem 1fr;
  }

  .coverage-count {
    grid-column: 2;
    text-align: right;
  }
}
</style>
