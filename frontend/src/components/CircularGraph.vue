<script setup lang="ts">
import { computed } from 'vue'
import { VueFlow, Handle, Position, MarkerType, type Node, type Edge } from '@vue-flow/core'
import type { CircularDetail } from '@/lib/api'
import '@vue-flow/core/dist/style.css'

const props = defineProps<{ circular: CircularDetail }>()
const emit = defineEmits<{ navigate: [id: string] }>()

const NODE_W = 210
const CX = 520
const CY = 300
const H_GAP = 370
const V_GAP = 200
const V_STEP = 120
const H_STEP = 240

interface NodeData {
  label: string
  status: string | null
  resolved: boolean
  isCurrent: boolean
  circularId: string | null
}

function makeNode(id: string, cx: number, cy: number, data: NodeData, type: string): Node<NodeData> {
  return { id, type, position: { x: cx - NODE_W / 2, y: cy - 40 }, data }
}

function spreadV(count: number, i: number): number {
  return CY + (i - (count - 1) / 2) * V_STEP
}

function spreadH(count: number, i: number): number {
  return CX + (i - (count - 1) / 2) * H_STEP
}

const graphNodes = computed<Node<NodeData>[]>(() => {
  const nodes: Node<NodeData>[] = []
  const c = props.circular

  nodes.push(makeNode('curr', CX, CY, {
    label: c.reference || c.title,
    status: c.status,
    resolved: true,
    isCurrent: true,
    circularId: c.id,
  }, 'current'))

  const outAmends = c.relationships.outgoing.filter(r => r.type === 'amends')
  outAmends.forEach((rel, i) => {
    nodes.push(makeNode(`out-amends-${i}`, CX - H_GAP, spreadV(outAmends.length, i), {
      label: rel.target?.reference || rel.target?.title || rel.target_reference || 'Unknown',
      status: rel.target?.status || null,
      resolved: Boolean(rel.target_id),
      isCurrent: false,
      circularId: rel.target_id || null,
    }, 'related'))
  })

  const inAmends = c.relationships.incoming.filter(r => r.type === 'amends')
  inAmends.forEach((rel, i) => {
    nodes.push(makeNode(`in-amends-${i}`, CX + H_GAP, spreadV(inAmends.length, i), {
      label: rel.source?.reference || rel.source?.title || 'Unknown',
      status: rel.source?.status || null,
      resolved: Boolean(rel.source_id),
      isCurrent: false,
      circularId: rel.source_id || null,
    }, 'related'))
  })

  const otherOut = c.relationships.outgoing.filter(r => r.type !== 'amends')
  otherOut.forEach((rel, i) => {
    nodes.push(makeNode(`out-other-${i}`, spreadH(otherOut.length, i), CY - V_GAP, {
      label: rel.target?.reference || rel.target?.title || rel.target_reference || 'Unknown',
      status: rel.target?.status || null,
      resolved: Boolean(rel.target_id),
      isCurrent: false,
      circularId: rel.target_id || null,
    }, 'related'))
  })

  const otherIn = c.relationships.incoming.filter(r => r.type !== 'amends')
  otherIn.forEach((rel, i) => {
    nodes.push(makeNode(`in-other-${i}`, spreadH(otherIn.length, i), CY + V_GAP, {
      label: rel.source?.reference || rel.source?.title || 'Unknown',
      status: rel.source?.status || null,
      resolved: Boolean(rel.source_id),
      isCurrent: false,
      circularId: rel.source_id || null,
    }, 'related'))
  })

  return nodes
})

const graphEdges = computed<Edge[]>(() => {
  const edges: Edge[] = []
  const c = props.circular

  function edgeBase(color: string): Partial<Edge> {
    return {
      type: 'smoothstep',
      markerEnd: { type: MarkerType.ArrowClosed, color, width: 16, height: 16 },
      style: { stroke: color, strokeWidth: 1.5 },
      labelStyle: { fill: color, fontSize: '10px', fontWeight: '600', fontFamily: 'inherit' },
      labelBgStyle: { fill: 'var(--sbp-surface)', fillOpacity: 0.92 },
      labelBgPadding: [4, 6] as [number, number],
      labelBgBorderRadius: 4,
    }
  }

  c.relationships.outgoing.filter(r => r.type === 'amends').forEach((_, i) => {
    edges.push({ id: `e-out-amends-${i}`, source: 'curr', target: `out-amends-${i}`, label: 'Amends', ...edgeBase('var(--sbp-green)') })
  })

  c.relationships.incoming.filter(r => r.type === 'amends').forEach((_, i) => {
    edges.push({ id: `e-in-amends-${i}`, source: `in-amends-${i}`, target: 'curr', label: 'Amends', ...edgeBase('var(--sbp-green)') })
  })

  c.relationships.outgoing.filter(r => r.type !== 'amends').forEach((rel, i) => {
    const label = rel.type.replace(/_/g, ' ').replace(/\b\w/g, ch => ch.toUpperCase())
    edges.push({ id: `e-out-other-${i}`, source: 'curr', target: `out-other-${i}`, label, ...edgeBase('var(--sbp-gold)') })
  })

  c.relationships.incoming.filter(r => r.type !== 'amends').forEach((rel, i) => {
    const label = rel.type.replace(/_/g, ' ').replace(/\b\w/g, ch => ch.toUpperCase())
    edges.push({ id: `e-in-other-${i}`, source: `in-other-${i}`, target: 'curr', label, ...edgeBase('var(--sbp-gold)') })
  })

  return edges
})

function statusColor(status: string | null): string {
  const s = (status || '').toLowerCase()
  if (s.includes('active') || s.includes('indexed')) return 'var(--p-green-500)'
  if (s.includes('superseded') || s.includes('replaced') || s.includes('amended')) return 'var(--sbp-gold)'
  if (s.includes('withdrawn') || s.includes('cancel')) return 'var(--p-red-400)'
  return 'var(--sbp-muted)'
}

function onNodeClick({ node }: { node: Node<NodeData> }) {
  if (node.data.circularId && !node.data.isCurrent) {
    emit('navigate', node.data.circularId)
  }
}
</script>

<template>
  <div class="cg-root">
    <VueFlow
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
      <template #node-current="{ data }">
        <div class="cg-node cg-node--current">
          <Handle type="source" :position="Position.Left" class="cg-handle" />
          <Handle type="target" :position="Position.Left" class="cg-handle" />
          <Handle type="source" :position="Position.Right" class="cg-handle" />
          <Handle type="target" :position="Position.Right" class="cg-handle" />
          <Handle type="source" :position="Position.Top" class="cg-handle" />
          <Handle type="target" :position="Position.Top" class="cg-handle" />
          <Handle type="source" :position="Position.Bottom" class="cg-handle" />
          <Handle type="target" :position="Position.Bottom" class="cg-handle" />
          <div class="cg-node-label">{{ data.label }}</div>
          <div v-if="data.status" class="cg-node-status" :style="{ color: statusColor(data.status) }">{{ data.status }}</div>
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
          <Handle type="source" :position="Position.Left" class="cg-handle" />
          <Handle type="target" :position="Position.Left" class="cg-handle" />
          <Handle type="source" :position="Position.Right" class="cg-handle" />
          <Handle type="target" :position="Position.Right" class="cg-handle" />
          <Handle type="source" :position="Position.Top" class="cg-handle" />
          <Handle type="target" :position="Position.Top" class="cg-handle" />
          <Handle type="source" :position="Position.Bottom" class="cg-handle" />
          <Handle type="target" :position="Position.Bottom" class="cg-handle" />
          <div class="cg-node-label">{{ data.label }}</div>
          <div v-if="data.status" class="cg-node-status" :style="{ color: statusColor(data.status) }">{{ data.status }}</div>
        </div>
      </template>
    </VueFlow>

    <div class="cg-legend">
      <span class="cg-legend-item"><span class="cg-legend-dot cg-legend-dot--green" /> Amends</span>
      <span class="cg-legend-item"><span class="cg-legend-dot cg-legend-dot--gold" /> Other</span>
      <span class="cg-legend-item cg-legend-muted"><i class="pi pi-arrows-h" style="font-size:0.7rem"/> Pan &nbsp;·&nbsp; <i class="pi pi-search-plus" style="font-size:0.7rem" /> Scroll to zoom</span>
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
}

:deep(.vue-flow__edge-label) {
  pointer-events: none;
}

.cg-node {
  width: 210px;
  padding: 10px 14px;
  border-radius: 8px;
  border: 1.5px solid var(--sbp-border);
  background: var(--sbp-surface);
  font-family: inherit;
  transition: border-color 0.15s, background 0.15s;
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

.cg-node-status {
  font-size: 0.62rem;
  font-weight: 600;
  text-transform: uppercase;
  margin-top: 5px;
  letter-spacing: 0.03em;
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

.cg-legend-dot--green { background: var(--sbp-green); }
.cg-legend-dot--gold  { background: var(--sbp-gold); }
</style>
