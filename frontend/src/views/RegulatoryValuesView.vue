<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import InputNumber from 'primevue/inputnumber'
import Select from 'primevue/select'
import Checkbox from 'primevue/checkbox'
import Message from 'primevue/message'
import ProgressSpinner from 'primevue/progressspinner'
import {
  queryCircularEntities,
  type CircularEntity,
  type EntityQueryParams,
  type EntityType,
} from '@/lib/api'

const router = useRouter()

const ENTITY_TYPE_OPTIONS: Array<{ label: string; value: EntityType | '' }> = [
  { label: 'Any type', value: '' },
  { label: 'Ratio', value: 'ratio' },
  { label: 'Monetary threshold', value: 'monetary_threshold' },
  { label: 'Percentage limit', value: 'percentage_limit' },
  { label: 'Numeric limit', value: 'numeric_limit' },
  { label: 'Deadline', value: 'deadline' },
  { label: 'Effective date', value: 'effective_date' },
]

const UNIT_OPTIONS = [
  { label: 'Any unit', value: '' },
  { label: '% (percent)', value: '%' },
  { label: 'PKR', value: 'PKR' },
  { label: 'USD', value: 'USD' },
  { label: 'times', value: 'times' },
  { label: 'days', value: 'days' },
  { label: 'months', value: 'months' },
]

const COMPARATOR_OPTIONS = [
  { label: 'Any', value: '' },
  { label: 'Minimum (≥)', value: 'min' },
  { label: 'Maximum (≤)', value: 'max' },
  { label: 'Exactly', value: 'exactly' },
  { label: 'Range', value: 'range' },
]

const metric = ref('')
const subject = ref('')
const department = ref('')
const entityType = ref<EntityType | ''>('')
const unit = ref('')
const comparator = ref('')
const minValue = ref<number | null>(null)
const maxValue = ref<number | null>(null)
const currentOnly = ref(false)

const results = ref<CircularEntity[]>([])
const total = ref(0)
const loading = ref(false)
const errorMessage = ref('')
const hasSearched = ref(false)

function formatDate(value?: string | null): string {
  if (!value) return ''
  return new Intl.DateTimeFormat(undefined, { year: 'numeric', month: 'short', day: '2-digit' }).format(new Date(value))
}

function displayValue(entity: CircularEntity): string {
  const prefix = entity.comparator === 'min' ? '≥ ' : entity.comparator === 'max' ? '≤ ' : ''
  if (entity.value_text) return `${prefix}${entity.value_text}`.trim()
  if (entity.effective_date) return formatDate(entity.effective_date)
  return entity.value_numeric != null ? `${prefix}${entity.value_numeric}` : '—'
}

async function runQuery() {
  loading.value = true
  errorMessage.value = ''
  hasSearched.value = true
  const params: EntityQueryParams = {
    metric: metric.value.trim() || undefined,
    subject: subject.value.trim() || undefined,
    department: department.value.trim() || undefined,
    entity_type: entityType.value || undefined,
    unit: unit.value || undefined,
    comparator: comparator.value || undefined,
    min_value: minValue.value ?? undefined,
    max_value: maxValue.value ?? undefined,
    current_only: currentOnly.value || undefined,
    per_page: 100,
  }
  try {
    const response = await queryCircularEntities(params)
    results.value = response.results
    total.value = response.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'Query failed.'
    results.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

function resetFilters() {
  metric.value = ''
  subject.value = ''
  department.value = ''
  entityType.value = ''
  unit.value = ''
  comparator.value = ''
  minValue.value = null
  maxValue.value = null
  currentOnly.value = false
}

function openCircular(id?: string) {
  if (!id) return
  void router.push({ path: `/circulars/${id}` })
}
</script>

<template>
  <section class="values-screen">
    <header class="values-header">
      <h1><i class="pi pi-percentage" /> Regulatory Values</h1>
      <p>Query the structured database of ratios, thresholds, limits, and deadlines extracted from circulars.</p>
    </header>

    <form class="values-filters" @submit.prevent="runQuery">
      <div class="filter-field">
        <label>Metric</label>
        <InputText v-model="metric" placeholder="CAR, LCR, Paid-up Capital…" />
      </div>
      <div class="filter-field">
        <label>Applies to</label>
        <InputText v-model="subject" placeholder="MFB, banks, DFIs…" />
      </div>
      <div class="filter-field">
        <label>Type</label>
        <Select v-model="entityType" :options="ENTITY_TYPE_OPTIONS" option-label="label" option-value="value" />
      </div>
      <div class="filter-field">
        <label>Unit</label>
        <Select v-model="unit" :options="UNIT_OPTIONS" option-label="label" option-value="value" />
      </div>
      <div class="filter-field">
        <label>Comparator</label>
        <Select v-model="comparator" :options="COMPARATOR_OPTIONS" option-label="label" option-value="value" />
      </div>
      <div class="filter-field">
        <label>Min value</label>
        <InputNumber v-model="minValue" :use-grouping="false" placeholder="e.g. 10" />
      </div>
      <div class="filter-field">
        <label>Max value</label>
        <InputNumber v-model="maxValue" :use-grouping="false" />
      </div>
      <div class="filter-field">
        <label>Department</label>
        <InputText v-model="department" placeholder="BPRD, Exchange Policy…" />
      </div>
      <div class="filter-field filter-checkbox">
        <Checkbox v-model="currentOnly" input-id="current-only" binary />
        <label for="current-only">Current only (exclude superseded; latest per metric)</label>
      </div>
      <div class="filter-actions">
        <Button type="submit" label="Search" icon="pi pi-search" :loading="loading" />
        <Button type="button" label="Reset" icon="pi pi-times" severity="secondary" text @click="resetFilters" />
      </div>
    </form>

    <Message v-if="errorMessage" severity="error" :closable="false">{{ errorMessage }}</Message>

    <div v-if="loading" class="values-loading"><ProgressSpinner /><span>Querying…</span></div>

    <template v-else-if="hasSearched">
      <p class="values-count">{{ total.toLocaleString() }} value{{ total === 1 ? '' : 's' }} found</p>
      <table v-if="results.length" class="values-table">
        <thead>
          <tr>
            <th>Metric</th>
            <th>Value</th>
            <th>Applies to</th>
            <th>Effective</th>
            <th>Circular</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="entity in results" :key="entity.id">
            <td class="col-metric">{{ entity.metric || '—' }}</td>
            <td class="col-value">
              {{ displayValue(entity) }}
              <span v-if="entity.unit && entity.unit !== '%' && !entity.value_text?.includes(entity.unit)" class="unit">{{ entity.unit }}</span>
            </td>
            <td>{{ entity.subject || '' }}</td>
            <td class="col-date">{{ entity.effective_date ? formatDate(entity.effective_date) : '' }}</td>
            <td>
              <button type="button" class="circular-link" @click="openCircular(entity.circular?.id)">
                {{ entity.circular?.reference || entity.circular?.title || 'Open' }}
              </button>
              <span
                v-if="entity.circular && entity.circular.status !== 'active'"
                class="status-flag"
              >{{ entity.circular.status }}</span>
            </td>
          </tr>
        </tbody>
      </table>
      <Message v-else severity="info" :closable="false">
        No values matched. Try broadening the filters, or generate "Regulatory Values" on more circulars.
      </Message>
    </template>

    <Message v-else severity="info" :closable="false">
      Set filters and search. Examples: unit "%", comparator "Minimum", min value 10 → thresholds above 10%.
      Or metric "Paid-up Capital", applies to "MFB", current only → the current minimum capital for MFBs.
    </Message>
  </section>
</template>

<style scoped>
.values-screen {
  padding: 1.25rem 1.5rem 2.5rem;
  max-width: 1100px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 1.1rem;
}

.values-header h1 {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin: 0 0 0.25rem;
}

.values-header p {
  margin: 0;
  color: var(--p-text-muted-color, #6b7280);
}

.values-filters {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0.75rem 1rem;
  align-items: end;
  padding: 1rem;
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.75rem;
  background: var(--p-content-background, #fff);
}

.filter-field {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}

.filter-field > label {
  font-size: 0.74rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  color: var(--p-text-muted-color, #6b7280);
}

.filter-field :deep(.p-inputtext),
.filter-field :deep(.p-select),
.filter-field :deep(.p-inputnumber) {
  width: 100%;
}

.filter-checkbox {
  flex-direction: row;
  align-items: center;
  gap: 0.5rem;
  grid-column: 1 / -1;
}

.filter-checkbox > label {
  text-transform: none;
  font-weight: 500;
  font-size: 0.85rem;
  color: var(--p-text-color, inherit);
}

.filter-actions {
  grid-column: 1 / -1;
  display: flex;
  gap: 0.5rem;
}

.values-loading {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  color: var(--p-text-muted-color, #6b7280);
}

.values-count {
  margin: 0;
  font-weight: 600;
}

.values-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.88rem;
}

.values-table th,
.values-table td {
  padding: 0.5rem 0.6rem;
  text-align: left;
  vertical-align: top;
  border-bottom: 1px solid var(--p-content-border-color, #e5e7eb);
}

.values-table thead th {
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--p-text-muted-color, #6b7280);
}

.col-metric { font-weight: 600; white-space: nowrap; }
.col-value { font-variant-numeric: tabular-nums; white-space: nowrap; }
.col-date { white-space: nowrap; color: var(--p-text-muted-color, #6b7280); }
.unit { color: var(--p-text-muted-color, #6b7280); margin-left: 0.2rem; }

.circular-link {
  background: none;
  border: none;
  padding: 0;
  color: var(--p-primary-color, #2563eb);
  cursor: pointer;
  text-align: left;
  font: inherit;
}

.circular-link:hover { text-decoration: underline; }

.status-flag {
  margin-left: 0.4rem;
  font-size: 0.72rem;
  text-transform: uppercase;
  color: #b45309;
}

@media (max-width: 820px) {
  .values-filters {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
