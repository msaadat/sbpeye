<script setup lang="ts">
import { computed, ref } from 'vue'

defineOptions({ name: 'JsonInspector' })

const props = withDefaults(defineProps<{
  value: unknown
  depth?: number
  name?: string
  root?: boolean
}>(), { depth: 0, name: '', root: true })

const isContainer = computed(() => props.value !== null && typeof props.value === 'object')
const entries = computed<[string, unknown][]>(() => {
  if (!isContainer.value) return []
  return Object.entries(props.value as Record<string, unknown>)
})
const expanded = ref(props.depth < 1 && entries.value.length <= 24)
const rawVisible = ref(false)
const stringExpanded = ref(false)
const copied = ref(false)
const kind = computed(() => Array.isArray(props.value) ? 'array' : typeof props.value)
const countLabel = computed(() => `${entries.value.length} ${Array.isArray(props.value) ? 'items' : 'keys'}`)
const formatted = computed(() => JSON.stringify(props.value, null, 2))
const primitiveText = computed(() => {
  if (props.value === null) return 'null'
  if (typeof props.value === 'string') {
    if (!stringExpanded.value && props.value.length > 360) {
      return JSON.stringify(`${props.value.slice(0, 360)}…`)
    }
    return JSON.stringify(props.value)
  }
  return String(props.value)
})

async function copyValue() {
  const text = typeof props.value === 'string' ? props.value : JSON.stringify(props.value, null, 2)
  await navigator.clipboard.writeText(text ?? 'undefined')
  copied.value = true
  window.setTimeout(() => { copied.value = false }, 1200)
}
</script>

<template>
  <div class="json-inspector" :class="{ 'is-root': root }">
    <div v-if="isContainer" class="json-node">
      <button
        class="json-disclosure"
        type="button"
        :aria-expanded="expanded"
        @click="expanded = !expanded"
      >
        <span :class="expanded ? 'pi pi-chevron-down' : 'pi pi-chevron-right'" />
        <span v-if="name" class="json-key">{{ name }}:</span>
        <span class="json-bracket">{{ Array.isArray(value) ? '[' : '{' }}</span>
        <span v-if="!expanded" class="json-count">{{ countLabel }}</span>
      </button>
      <div v-if="expanded" class="json-children">
        <JsonInspector
          v-for="([key, child], index) in entries"
          :key="key"
          :value="child"
          :name="Array.isArray(value) ? String(index) : key"
          :depth="depth + 1"
          :root="false"
        />
      </div>
      <span v-if="expanded" class="json-bracket json-close">{{ Array.isArray(value) ? ']' : '}' }}</span>
    </div>
    <div v-else class="json-primitive-row">
      <span v-if="name" class="json-key">{{ name }}:</span>
      <span class="json-primitive" :class="`is-${kind}`">{{ primitiveText }}</span>
      <button
        v-if="typeof value === 'string' && value.length > 360"
        class="json-inline-action"
        type="button"
        @click="stringExpanded = !stringExpanded"
      >{{ stringExpanded ? 'Collapse' : 'Expand' }}</button>
    </div>
    <div v-if="root" class="json-actions">
      <button type="button" @click="copyValue">{{ copied ? 'Copied' : 'Copy' }}</button>
      <button type="button" @click="rawVisible = !rawVisible">{{ rawVisible ? 'Hide raw JSON' : 'Raw JSON' }}</button>
    </div>
    <pre v-if="root && rawVisible" class="json-raw">{{ formatted }}</pre>
  </div>
</template>
