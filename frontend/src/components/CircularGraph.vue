<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { VueFlow, Handle, Position, MarkerType, BaseEdge, type Node, type Edge } from '@vue-flow/core'
import { getCircularDetail, type CircularDetail } from '@/lib/api'
import '@vue-flow/core/dist/style.css'

const props = defineProps<{ circular: CircularDetail }>()
const emit = defineEmits<{ navigate: [id: string]; focuschange: [label: string] }>()

// the graph re-centers on whichever circular is focused; clicking a node pushes
// onto this stack (traverse), the back button pops it. props.circular is the root.
const focusStack = ref<CircularDetail[]>([props.circular])
const source = computed(() => focusStack.value[focusStack.value.length - 1])
const loading = ref(false)

watch(() => props.circular, c => { focusStack.value = [c] })

async function traverseTo(id: string) {
  if (loading.value || id === source.value.id) return
  loading.value = true
  try {
    const detail = await getCircularDetail(id)
    focusStack.value = [...focusStack.value, detail]
    emit('focuschange', detail.reference || detail.title)
  } catch {
    emit('navigate', id)
  } finally {
    loading.value = false
  }
}

function goBack() {
  if (focusStack.value.length <= 1) return
  focusStack.value = focusStack.value.slice(0, -1)
  const s = source.value
  emit('focuschange', s.reference || s.title)
}

function openCircular(id: string) {
  emit('navigate', id)
}

const NODE_W = 210
const COL_PITCH = 300
const ROW_PITCH = 104
// past this many nodes, collapse to one column per year with vertical stacks
const MAX_SINGLE_ROW = 10

interface NodeData {
  label: string
  status: string | null
  dateLabel: string | null
  resolved: boolean
  isCurrent: boolean
  circularId: string | null
}

interface TimelineEntry {
  key: string
  label: string
  status: string | null
  resolved: boolean
  circularId: string | null
  sortTime: number | null
  dateLabel: string | null
  isCurrent: boolean
  rels: { type: string; dir: 'out' | 'in' }[]
}

const TYPE_STYLES: Record<string, { label: string; color: string }> = {
  amends: { label: 'Amends', color: 'var(--sbp-green)' },
  adds_to: { label: 'Adds to', color: 'var(--p-blue-500)' },
  supersedes: { label: 'Supersedes', color: 'var(--p-orange-500)' },
  clarifies: { label: 'Clarifies', color: 'var(--p-purple-400)' },
  cancels: { label: 'Cancels', color: 'var(--p-red-500)' },
}

function typeStyle(type: string): { label: string; color: string } {
  return TYPE_STYLES[type] ?? {
    label: type.replace(/_/g, ' ').replace(/^\w/, ch => ch.toUpperCase()),
    color: 'var(--sbp-gold)',
  }
}

function parseNodeDate(dateStr: string | null | undefined, reference: string): { sortTime: number | null; dateLabel: string | null } {
  if (dateStr) {
    const d = new Date(dateStr)
    if (!Number.isNaN(d.getTime())) {
      return { sortTime: d.getTime(), dateLabel: d.toLocaleDateString('en-US', { month: 'short', year: 'numeric' }) }
    }
  }
  // references reliably embed the year, e.g. "EPD Circular Letter No. 16 of 2019"
  const years = reference.match(/\b(19|20)\d{2}\b/g)
  if (years && years.length > 0) {
    const y = Number(years[years.length - 1])
    return { sortTime: new Date(y, 5, 30).getTime(), dateLabel: String(y) }
  }
  return { sortTime: null, dateLabel: null }
}

const relatedEntries = computed<TimelineEntry[]>(() => {
  const c = source.value
  const map = new Map<string, TimelineEntry>()

  function upsert(other: { id?: string; reference?: string | null; title?: string | null; status?: string | null; date?: string | null } | null | undefined, otherId: string | null | undefined, fallbackRef: string | null | undefined, type: string, dir: 'out' | 'in') {
    if (otherId && otherId === c.id) return
    const label = other?.reference || other?.title || fallbackRef || 'Unknown'
    const key = otherId || `ref:${label.trim().toLowerCase()}`
    let entry = map.get(key)
    if (!entry) {
      const { sortTime, dateLabel } = parseNodeDate(other?.date, label)
      entry = {
        key,
        label,
        status: other?.status ?? null,
        resolved: Boolean(otherId),
        circularId: otherId ?? null,
        sortTime,
        dateLabel,
        isCurrent: false,
        rels: [],
      }
      map.set(key, entry)
    }
    entry.rels.push({ type, dir })
  }

  c.relationships.outgoing.forEach(r => upsert(r.target, r.target_id, r.target_reference, r.type, 'out'))
  c.relationships.incoming.forEach(r => upsert(r.source, r.source_id, null, r.type, 'in'))
  return [...map.values()]
})

const layout = computed(() => {
  const c = source.value
  const currentRef = c.reference || c.title
  const current: TimelineEntry = {
    key: '__current__',
    label: currentRef,
    status: c.status,
    resolved: true,
    circularId: c.id,
    ...parseNodeDate(c.date, currentRef),
    isCurrent: true,
    rels: [],
  }

  const sorted = [current, ...relatedEntries.value]
    .sort((a, b) => (a.sortTime ?? -Infinity) - (b.sortTime ?? -Infinity))

  const colOf = new Map<string, number>()
  const rowOf = new Map<string, number>()

  if (sorted.length <= MAX_SINGLE_ROW) {
    sorted.forEach((e, i) => {
      colOf.set(e.key, i)
      rowOf.set(e.key, 0)
    })
  } else {
    const groups = new Map<string, TimelineEntry[]>()
    sorted.forEach(e => {
      const year = e.sortTime != null ? String(new Date(e.sortTime).getFullYear()) : 'unknown'
      const group = groups.get(year)
      if (group) group.push(e)
      else groups.set(year, [e])
    })
    let col = 0
    for (const group of groups.values()) {
      const ordered = [...group.filter(e => e.isCurrent), ...group.filter(e => !e.isCurrent)]
      ordered.forEach((e, row) => {
        colOf.set(e.key, col)
        rowOf.set(e.key, row)
      })
      col++
    }
  }

  return { sorted, colOf, rowOf }
})

const graphNodes = computed<Node<NodeData>[]>(() => {
  const { sorted, colOf, rowOf } = layout.value
  return sorted.map(e => ({
    id: e.key,
    type: e.isCurrent ? 'current' : 'related',
    position: {
      x: (colOf.get(e.key) ?? 0) * COL_PITCH,
      y: (rowOf.get(e.key) ?? 0) * ROW_PITCH,
    },
    data: {
      label: e.label,
      status: e.status,
      dateLabel: e.dateLabel,
      resolved: e.resolved,
      isCurrent: e.isCurrent,
      circularId: e.circularId,
    },
  }))
})

const graphEdges = computed<Edge[]>(() => {
  const { colOf } = layout.value
  const edges: Edge[] = []
  const pairCount = new Map<string, number>()
  const currentCol = colOf.get('__current__') ?? 0

  relatedEntries.value.forEach(entry => {
    entry.rels.forEach((rel, i) => {
      const dup = pairCount.get(entry.key) ?? 0
      pairCount.set(entry.key, dup + 1)
      const span = Math.max(1, Math.abs((colOf.get(entry.key) ?? 0) - currentCol))
      const { color } = typeStyle(rel.type)
      edges.push({
        id: `e-${entry.key}-${rel.type}-${rel.dir}-${i}`,
        source: rel.dir === 'out' ? '__current__' : entry.key,
        target: rel.dir === 'out' ? entry.key : '__current__',
        sourceHandle: 'top-s',
        targetHandle: 'top-t',
        type: 'arc',
        data: { arcHeight: 120 * Math.sqrt(span) + dup * 56 },
        markerEnd: { type: MarkerType.ArrowClosed, color, width: 16, height: 16 },
        style: { stroke: color, strokeWidth: 1.5 },
      })
    })
  })

  return edges
})

const legendItems = computed(() => {
  const seen = new Map<string, { label: string; color: string }>()
  relatedEntries.value.forEach(e => {
    e.rels.forEach(r => {
      const style = typeStyle(r.type)
      seen.set(style.label, style)
    })
  })
  return [...seen.values()]
})

function arcPath(p: { sourceX: number; sourceY: number; targetX: number; targetY: number; data?: { arcHeight?: number } }): string {
  const h = p.data?.arcHeight ?? 120
  if (Math.abs(p.sourceX - p.targetX) < 1) {
    // same column (stacked): bulge sideways instead of arcing through the stack
    const my = (p.sourceY + p.targetY) / 2
    return `M ${p.sourceX},${p.sourceY} Q ${p.sourceX - NODE_W * 0.85},${my} ${p.targetX},${p.targetY}`
  }
  const mx = (p.sourceX + p.targetX) / 2
  const my = Math.min(p.sourceY, p.targetY) - h
  return `M ${p.sourceX},${p.sourceY} Q ${mx},${my} ${p.targetX},${p.targetY}`
}

function statusColor(status: string | null): string {
  const s = (status || '').toLowerCase()
  if (s.includes('active') || s.includes('indexed')) return 'var(--p-green-500)'
  if (s.includes('superseded') || s.includes('replaced') || s.includes('amended')) return 'var(--sbp-gold)'
  if (s.includes('withdrawn') || s.includes('cancel')) return 'var(--p-red-400)'
  return 'var(--sbp-muted)'
}

function onNodeClick({ node }: { node: Node<NodeData> }) {
  const data = node.data
  if (data?.circularId && !data.isCurrent) {
    traverseTo(data.circularId)
  }
}
</script>

<template>
  <div class="cg-root">
    <button v-if="focusStack.length > 1" class="cg-back" @click="goBack">
      <i class="pi pi-arrow-left" style="font-size:0.72rem" /> Back
    </button>
    <div v-if="loading" class="cg-loading"><i class="pi pi-spinner pi-spin" /></div>
    <VueFlow
      :key="source.id"
      :nodes="graphNodes"
      :edges="graphEdges"
      :nodes-draggable="false"
      :nodes-connectable="false"
      :elements-selectable="false"
      :fit-view-on-init="true"
      :min-zoom="0.25"
      :max-zoom="2"
      class="cg-flow"
      @node-click="onNodeClick"
    >
      <template #edge-arc="p">
        <BaseEdge :path="arcPath(p)" :marker-end="p.markerEnd" :style="p.style" />
      </template>
      <template #node-current="{ data }">
        <div class="cg-node cg-node--current">
          <Handle id="top-s" type="source" :position="Position.Top" class="cg-handle" />
          <Handle id="top-t" type="target" :position="Position.Top" class="cg-handle" />
          <button
            v-if="data.circularId"
            class="cg-open-btn"
            title="Open this circular"
            aria-label="Open this circular"
            @click.stop="openCircular(data.circularId)"
          ><i class="pi pi-external-link" /></button>
          <div class="cg-node-label">{{ data.label }}</div>
          <div class="cg-node-meta">
            <span v-if="data.dateLabel" class="cg-node-date">{{ data.dateLabel }}</span>
            <span v-if="data.status" class="cg-node-status" :style="{ color: statusColor(data.status) }">{{ data.status }}</span>
          </div>
        </div>
      </template>
      <template #node-related="{ data }">
        <div
          class="cg-node cg-node--related"
          :class="{
            'cg-node--unresolved': !data.resolved,
            'cg-node--clickable': data.resolved && data.circularId,
          }"
        >
          <Handle id="top-s" type="source" :position="Position.Top" class="cg-handle" />
          <Handle id="top-t" type="target" :position="Position.Top" class="cg-handle" />
          <button
            v-if="data.resolved && data.circularId"
            class="cg-open-btn"
            title="Open this circular"
            aria-label="Open this circular"
            @click.stop="openCircular(data.circularId)"
          ><i class="pi pi-external-link" /></button>
          <div class="cg-node-label">{{ data.label }}</div>
          <div class="cg-node-meta">
            <span v-if="data.dateLabel" class="cg-node-date">{{ data.dateLabel }}</span>
            <span v-if="data.status" class="cg-node-status" :style="{ color: statusColor(data.status) }">{{ data.status }}</span>
          </div>
        </div>
      </template>
    </VueFlow>

    <div class="cg-legend">
      <span v-for="item in legendItems" :key="item.label" class="cg-legend-item">
        <span class="cg-legend-dot" :style="{ background: item.color }" /> {{ item.label }}
      </span>
      <span class="cg-legend-item cg-legend-muted">Oldest <i class="pi pi-arrow-right" style="font-size:0.6rem" /> newest</span>
      <span class="cg-legend-item cg-legend-muted">Click to explore &nbsp;·&nbsp; <i class="pi pi-external-link" style="font-size:0.68rem" /> to open</span>
    </div>
  </div>
</template>

<style scoped>
.cg-root {
  width: 100%;
  height: 100%;
  position: relative;
}

.cg-flow {
  width: 100%;
  height: 100%;
  background: var(--sbp-bg);
}

/* hide handle dots but keep them for edge routing */
:deep(.cg-handle) {
  opacity: 0;
  pointer-events: none;
  width: 1px;
  height: 1px;
  min-width: unset;
  min-height: unset;
  border: none;
  background: transparent;
}

:deep(.vue-flow__edge-path) {
  stroke-width: 1.5;
  fill: none;
}

.cg-node {
  position: relative;
  width: 210px;
  padding: 10px 14px;
  border-radius: 8px;
  border: 1.5px solid var(--sbp-border);
  background: var(--sbp-surface);
  font-family: inherit;
  transition: border-color 0.15s, background 0.15s;
}

.cg-open-btn {
  position: absolute;
  top: 6px;
  right: 6px;
  width: 22px;
  height: 22px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border-radius: 6px;
  border: 1px solid var(--sbp-border);
  background: var(--sbp-surface);
  color: var(--sbp-muted);
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.15s, color 0.15s, border-color 0.15s;
  font-size: 0.68rem;
}

.cg-node:hover .cg-open-btn {
  opacity: 1;
}

.cg-open-btn:hover {
  color: var(--sbp-green);
  border-color: var(--sbp-green);
}

.cg-node--current {
  border-color: var(--sbp-green);
  border-width: 2px;
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--sbp-green) 18%, transparent);
}

.cg-node--clickable {
  cursor: pointer;
}

.cg-node--clickable:hover {
  border-color: var(--sbp-green);
  background: color-mix(in srgb, var(--sbp-green) 5%, var(--sbp-surface));
}

.cg-node--unresolved {
  opacity: 0.45;
  border-style: dashed;
}

.cg-node-label {
  font-size: 0.78rem;
  font-weight: 500;
  color: var(--sbp-text);
  line-height: 1.35;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.cg-node-meta {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-top: 5px;
}

.cg-node-date {
  font-size: 0.66rem;
  font-weight: 500;
  color: var(--sbp-muted);
}

.cg-node-status {
  font-size: 0.62rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.03em;
}

.cg-back {
  position: absolute;
  top: 14px;
  left: 14px;
  z-index: 5;
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 6px 12px;
  border-radius: 999px;
  background: var(--sbp-surface);
  border: 1px solid var(--sbp-border);
  color: var(--sbp-text);
  font-size: 0.72rem;
  font-family: inherit;
  cursor: pointer;
  box-shadow: var(--sbp-shadow-sm);
  transition: border-color 0.15s, color 0.15s;
}

.cg-back:hover {
  border-color: var(--sbp-green);
  color: var(--sbp-green);
}

.cg-loading {
  position: absolute;
  top: 16px;
  right: 16px;
  z-index: 5;
  color: var(--sbp-green);
  font-size: 1rem;
}

.cg-legend {
  position: absolute;
  bottom: 14px;
  left: 14px;
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 6px 12px;
  border-radius: 999px;
  background: var(--sbp-surface);
  border: 1px solid var(--sbp-border);
  font-size: 0.72rem;
  color: var(--sbp-text);
  pointer-events: none;
  box-shadow: var(--sbp-shadow-sm);
}

.cg-legend-item {
  display: flex;
  align-items: center;
  gap: 5px;
}

.cg-legend-muted {
  color: var(--sbp-muted);
}

.cg-legend-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
  flex-shrink: 0;
}
</style>
