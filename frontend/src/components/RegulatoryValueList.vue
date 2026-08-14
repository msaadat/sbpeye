<script setup lang="ts">
/**
 * Extracted regulatory values, grouped by type.
 *
 * Identical for a circular and a regulation because the values are: `CircularEntity` is
 * one table with a `subject_kind` discriminator, so a CAR requirement renders the same
 * way whichever instrument stated it.
 */
import { computed } from 'vue'
import type { CircularEntity } from '@/lib/api'

const props = defineProps<{ entities: CircularEntity[] }>()

const TYPE_LABELS: Record<string, string> = {
  ratio: 'Ratios',
  monetary_threshold: 'Monetary thresholds',
  percentage_limit: 'Percentage limits',
  numeric_limit: 'Numeric limits',
  deadline: 'Deadlines',
  effective_date: 'Effective dates',
}

// Authority order, not alphabetical: a ratio is the headline, a date is a footnote.
const TYPE_ORDER = [
  'ratio',
  'monetary_threshold',
  'percentage_limit',
  'numeric_limit',
  'deadline',
  'effective_date',
]

const COMPARATOR_PREFIX: Record<string, string> = {
  min: '≥ ',
  max: '≤ ',
  exactly: '',
  range: '',
}

const groups = computed(() => {
  const byType = new Map<string, CircularEntity[]>()
  for (const entity of props.entities) {
    const list = byType.get(entity.entity_type) ?? []
    list.push(entity)
    byType.set(entity.entity_type, list)
  }
  return TYPE_ORDER.filter((type) => byType.has(type)).map((type) => ({
    type,
    label: TYPE_LABELS[type] ?? type,
    items: byType.get(type) as CircularEntity[],
  }))
})

function formatDate(value?: string | null): string {
  if (!value) return ''
  return new Intl.DateTimeFormat(undefined, {
    year: 'numeric', month: 'short', day: '2-digit',
  }).format(new Date(value))
}

function formatValue(entity: CircularEntity): string {
  if (entity.value_text) {
    const prefix = entity.comparator ? COMPARATOR_PREFIX[entity.comparator] ?? '' : ''
    return `${prefix}${entity.value_text}`.trim()
  }
  if (entity.effective_date) return formatDate(entity.effective_date)
  return entity.value_numeric != null ? String(entity.value_numeric) : '—'
}

/** A date already shown as the value must not repeat itself underneath. */
function showsEffectiveDate(entity: CircularEntity): boolean {
  return Boolean(
    entity.effective_date &&
      entity.entity_type !== 'deadline' &&
      entity.entity_type !== 'effective_date',
  )
}
</script>

<template>
  <section v-if="groups.length" class="detail-section entities-section">
    <h2><i class="pi pi-percentage section-icon" />Regulatory Values</h2>
    <div class="entity-groups">
      <div v-for="group in groups" :key="group.type" class="entity-group">
        <span class="entity-group-label">{{ group.label }}</span>
        <ul class="entity-list">
          <li v-for="entity in group.items" :key="entity.id" class="entity-item">
            <div class="entity-line">
              <span class="entity-metric">{{ entity.metric || '—' }}</span>
              <span class="entity-value">
                {{ formatValue(entity) }}
                <span
                  v-if="entity.unit && entity.unit !== '%' && !entity.value_text?.includes(entity.unit)"
                  class="entity-unit"
                >{{ entity.unit }}</span>
              </span>
            </div>
            <div v-if="entity.subject || showsEffectiveDate(entity)" class="entity-sub">
              <span v-if="entity.subject" class="entity-subject">{{ entity.subject }}</span>
              <span v-if="showsEffectiveDate(entity)" class="entity-effective">
                <i class="pi pi-calendar" /> {{ formatDate(entity.effective_date) }}
              </span>
            </div>
          </li>
        </ul>
      </div>
    </div>
  </section>
</template>

<style scoped>
.entity-groups {
  display: flex;
  flex-direction: column;
  gap: 0.85rem;
}

.entity-group-label {
  display: block;
  font-size: var(--sbp-fs-eyebrow);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--sbp-muted);
  margin-bottom: 0.25rem;
}

.entity-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.entity-item {
  padding: 0.3rem 0;
  border-top: 1px solid var(--sbp-border);
}

.entity-line {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 0.4rem;
  font-size: var(--sbp-fs-meta);
  line-height: 1.3;
}

.entity-metric {
  font-weight: 600;
  min-width: 0;
  overflow-wrap: anywhere;
}

.entity-value {
  flex: 0 1 auto;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  text-align: right;
  overflow-wrap: anywhere;
}

.entity-unit {
  color: var(--sbp-muted);
  font-weight: 400;
  margin-left: 0.2rem;
}

.entity-sub {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0.2rem 0.5rem;
  margin-top: 0.12rem;
  font-size: var(--sbp-fs-eyebrow);
  line-height: 1.3;
}

.entity-subject {
  color: var(--sbp-muted);
  overflow-wrap: anywhere;
}

.entity-effective {
  display: inline-flex;
  align-items: center;
  gap: 0.2rem;
  color: var(--sbp-muted);
  white-space: nowrap;
}
</style>
