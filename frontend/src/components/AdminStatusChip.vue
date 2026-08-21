<script setup lang="ts">
/** A status word in its tone. Shared so `failed` looks the same on every admin tab. */
import { computed } from 'vue'

import { humanize, statusTone } from '@/views/admin/adminFormat'

const props = defineProps<{ status: string; count?: number }>()

const tone = computed(() => statusTone(props.status))
</script>

<template>
  <span class="status-chip" :class="`tone-${tone}`">
    <span class="status-dot" aria-hidden="true" />
    {{ humanize(status) }}
    <span v-if="count !== undefined" class="status-count">{{ count.toLocaleString() }}</span>
  </span>
</template>

<style scoped>
.status-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  padding: 0.1rem 0.5rem;
  border: 1px solid var(--sbp-border);
  border-radius: var(--sbp-radius-pill);
  font-size: var(--sbp-fs-meta);
  color: var(--sbp-text);
  white-space: nowrap;
}

.status-dot {
  width: 0.45rem;
  height: 0.45rem;
  border-radius: 50%;
  background: var(--sbp-muted);
  flex: none;
}

.status-count {
  color: var(--sbp-muted);
  font-variant-numeric: tabular-nums;
}

.tone-ok .status-dot {
  background: var(--sbp-success);
}

.tone-warn .status-dot {
  background: var(--sbp-warning);
}

.tone-error .status-dot {
  background: var(--sbp-danger);
}

.tone-busy .status-dot {
  background: var(--sbp-green);
  animation: admin-status-pulse 1.4s ease-in-out infinite;
}

@keyframes admin-status-pulse {
  50% {
    opacity: 0.35;
  }
}

@media (prefers-reduced-motion: reduce) {
  .tone-busy .status-dot {
    animation: none;
  }
}
</style>
