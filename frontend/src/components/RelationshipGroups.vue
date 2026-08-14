<script setup lang="ts">
/**
 * Relationship pills, grouped by kind and direction, with a fold for long groups.
 *
 * The parent builds the groups, because a circular's edges and a law's have different
 * payload shapes and different words for the same direction ("Amended by" vs "Made under").
 * What is shared — and what lived twice before — is the rendering: the chip, the count,
 * the pills, and the threshold at which a group of forty stops being scannable.
 */
import { ref } from 'vue'

export interface RelationGroupItem {
  id: string | null
  label: string
  /** Optional muted line above the label, e.g. a part's container. */
  crumb?: string | null
}

export interface RelationGroup {
  key: string
  direction: 'outgoing' | 'incoming'
  /** The raw edge type. Not rendered — parents sort on it. */
  type?: string
  label: string
  items: RelationGroupItem[]
}

const props = withDefaults(
  defineProps<{ groups: RelationGroup[]; threshold?: number }>(),
  { threshold: 12 },
)
const emit = defineEmits<{ select: [id: string] }>()

const expanded = ref<Set<string>>(new Set())

function toggle(key: string) {
  const next = new Set(expanded.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  expanded.value = next
}

function visible(group: RelationGroup): RelationGroupItem[] {
  if (expanded.value.has(group.key) || group.items.length <= props.threshold) return group.items
  return group.items.slice(0, props.threshold)
}
</script>

<template>
  <div class="relationship-groups">
    <div
      v-for="group in props.groups"
      :key="group.key"
      class="relationship-group"
      :class="{ incoming: group.direction === 'incoming' }"
    >
      <span class="relationship-group-chip">
        {{ group.label }}<span class="relationship-group-count">{{ group.items.length }}</span>
      </span>
      <button
        v-for="(item, index) in visible(group)"
        :key="`${group.key}-${index}`"
        type="button"
        class="intelligence-pill relationship-ref-pill"
        :disabled="!item.id"
        :title="item.crumb || undefined"
        @click="item.id && emit('select', item.id)"
      >
        {{ item.label }}
      </button>
      <button
        v-if="group.items.length > props.threshold"
        type="button"
        class="relationship-show-more"
        @click="toggle(group.key)"
      >
        {{ expanded.has(group.key)
          ? 'Show less'
          : `+${group.items.length - props.threshold} more` }}
      </button>
    </div>
  </div>
</template>
