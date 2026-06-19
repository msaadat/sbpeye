<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import CircularDetailPane from '@/components/CircularDetailPane.vue'
import {
  downloadBatchZip,
  downloadCsvExport,
  getCircularDepartments,
  getCircularSearch,
  getCircularTags,
  type CircularSummary,
  type DepartmentCount,
  type SearchFilters,
  type TagCount,
} from '@/lib/api'

interface SelectOption<T = string> { label: string; value: T }

const route = useRoute()
const router = useRouter()
const toast = useToast()

const rows = ref<CircularSummary[]>([])
const selectedIds = ref<string[]>([])
const departments = ref<DepartmentCount[]>([])
const tags = ref<TagCount[]>([])
const loading = ref(false)
const optionsLoading = ref(false)
const exportLoading = ref(false)
const zipLoading = ref(false)
const errorMessage = ref('')
const totalRecords = ref(0)
const page = ref(1)
const perPage = ref(20)

const query = ref('')
const department = ref<string | null>(null)
const tag = ref<string | null>(null)
const startYear = ref<number | null>(null)
const endYear = ref<number | null>(null)
const sortBy = ref('relevance')
let searchController: AbortController | undefined

const selectedCircularId = computed(() => typeof route.params.id === 'string' ? route.params.id : '')
const totalPages = computed(() => Math.max(1, Math.ceil(totalRecords.value / perPage.value)))
const allPageSelected = computed(() => Boolean(rows.value.length) && rows.value.every((row) => selectedIds.value.includes(row.id)))
const hasFilters = computed(() => Boolean(query.value.trim() || department.value || tag.value || startYear.value || endYear.value))

const sortOptions: SelectOption[] = [
  { label: 'Relevance', value: 'relevance' },
  { label: 'Newest first', value: 'date' },
]
const pageSizeOptions = [
  { label: '20 / page', value: 20 },
  { label: '50 / page', value: 50 },
  { label: '100 / page', value: 100 },
]
const departmentOptions = computed<SelectOption[]>(() => [
  { label: 'All departments', value: '' },
  ...departments.value.filter((item) => item.department).map((item) => ({
    label: `${item.department} (${item.count.toLocaleString()})`, value: item.department || '',
  })),
])
const tagOptions = computed<SelectOption[]>(() => [
  { label: 'All tags', value: '' },
  ...tags.value.map((item) => ({ label: `${item.tag} (${item.count.toLocaleString()})`, value: item.tag })),
])
const filters = computed<SearchFilters>(() => ({
  q: query.value.trim(), start_year: startYear.value, end_year: endYear.value,
  department: department.value || null, sort_by: sortBy.value, tag: tag.value || null,
  page: page.value, per_page: perPage.value,
}))

function queryString(value: unknown): string {
  return Array.isArray(value) ? String(value[0] || '') : typeof value === 'string' ? value : ''
}

function readRouteFilters() {
  query.value = queryString(route.query.q)
  department.value = queryString(route.query.department) || null
  tag.value = queryString(route.query.tag) || null
  startYear.value = Number(queryString(route.query.start_year)) || null
  endYear.value = Number(queryString(route.query.end_year)) || null
  sortBy.value = queryString(route.query.sort_by) || 'relevance'
  page.value = Math.max(1, Number(queryString(route.query.page)) || 1)
  perPage.value = [20, 50, 100].includes(Number(queryString(route.query.per_page))) ? Number(queryString(route.query.per_page)) : 20
}

function routeQuery(): Record<string, string> {
  const result: Record<string, string> = {}
  if (query.value.trim()) result.q = query.value.trim()
  if (department.value) result.department = department.value
  if (tag.value) result.tag = tag.value
  if (startYear.value) result.start_year = String(startYear.value)
  if (endYear.value) result.end_year = String(endYear.value)
  if (sortBy.value !== 'relevance') result.sort_by = sortBy.value
  if (page.value > 1) result.page = String(page.value)
  if (perPage.value !== 20) result.per_page = String(perPage.value)
  return result
}

async function syncRoute() {
  await router.replace({ path: selectedCircularId.value ? `/circulars/${selectedCircularId.value}` : '/circulars', query: routeQuery() })
}

function formatDate(value?: string | null): string {
  if (!value) return 'Not dated'
  return new Intl.DateTimeFormat(undefined, { year: 'numeric', month: 'short', day: '2-digit' }).format(new Date(value))
}

function statusSeverity(status: string): 'success' | 'info' | 'warn' | 'danger' {
  const value = status.toLowerCase()
  if (value.includes('active') || value.includes('indexed')) return 'success'
  if (value.includes('superseded') || value.includes('replaced')) return 'warn'
  if (value.includes('withdrawn') || value.includes('cancel')) return 'danger'
  return 'info'
}

async function loadOptions() {
  optionsLoading.value = true
  try {
    ;[departments.value, tags.value] = await Promise.all([getCircularDepartments(), getCircularTags()])
  } catch (error) {
    toast.add({ severity: 'warn', summary: 'Filters unavailable', detail: error instanceof Error ? error.message : 'Unable to load filters.', life: 5000 })
  } finally { optionsLoading.value = false }
}

async function loadCirculars(resetPage = false) {
  if (resetPage) page.value = 1
  searchController?.abort()
  const controller = new AbortController()
  searchController = controller
  const timeout = window.setTimeout(() => controller.abort('timeout'), 30_000)
  loading.value = true
  errorMessage.value = ''
  try {
    const response = await getCircularSearch(filters.value, controller.signal)
    if (controller !== searchController) return
    rows.value = response.items
    totalRecords.value = response.total
    page.value = response.page
    perPage.value = response.per_page
    await syncRoute()
  } catch (error) {
    if (controller !== searchController) return
    rows.value = []
    totalRecords.value = 0
    errorMessage.value = controller.signal.aborted
      ? 'Search timed out. Refine the query and try again.'
      : error instanceof Error ? error.message : 'Unable to load circulars.'
  } finally {
    window.clearTimeout(timeout)
    if (controller === searchController) loading.value = false
  }
}

function clearFilters() {
  query.value = ''
  department.value = null
  tag.value = null
  startYear.value = null
  endYear.value = null
  sortBy.value = 'relevance'
  void loadCirculars(true)
}

function togglePageSelection() {
  if (allPageSelected.value) {
    const pageIds = new Set(rows.value.map((row) => row.id))
    selectedIds.value = selectedIds.value.filter((id) => !pageIds.has(id))
  } else {
    selectedIds.value = [...new Set([...selectedIds.value, ...rows.value.map((row) => row.id)])]
  }
}

function openCircular(id: string) {
  void router.push({ path: `/circulars/${id}`, query: routeQuery() })
}

function closeCircular() {
  void router.push({ path: '/circulars', query: routeQuery() })
}

async function exportCsv() {
  exportLoading.value = true
  try { await downloadCsvExport(filters.value) }
  catch (error) { toast.add({ severity: 'error', summary: 'CSV export failed', detail: error instanceof Error ? error.message : 'Unable to export CSV.', life: 6000 }) }
  finally { exportLoading.value = false }
}

async function downloadSelectedZip() {
  if (!selectedIds.value.length) return
  zipLoading.value = true
  try { await downloadBatchZip(selectedIds.value) }
  catch (error) { toast.add({ severity: 'error', summary: 'ZIP download failed', detail: error instanceof Error ? error.message : 'Unable to download circulars.', life: 6000 }) }
  finally { zipLoading.value = false }
}

function handoffToChat() {
  if (selectedIds.value.length) void router.push({ path: '/chat', query: { circular_ids: selectedIds.value.join(',') } })
}

function changePage(offset: number) {
  page.value = Math.min(totalPages.value, Math.max(1, page.value + offset))
  void loadCirculars()
}

watch(perPage, () => { if (!loading.value) void loadCirculars(true) })

onMounted(() => {
  readRouteFilters()
  void loadOptions()
  void loadCirculars()
})

onBeforeUnmount(() => searchController?.abort())
</script>

<template>
  <section class="circular-workspace" :class="{ 'has-detail': selectedCircularId }">
    <aside class="circular-filters">
      <div class="workspace-brandline">
        <div><span>Circulars</span><strong>Search workspace</strong></div>
        <Button icon="pi pi-filter-slash" text rounded aria-label="Reset filters" :disabled="!hasFilters" @click="clearFilters" />
      </div>

      <form class="filter-stack" @submit.prevent="loadCirculars(true)">
        <label><span>Search</span><span class="p-input-icon-left"><i class="pi pi-search" /><InputText v-model="query" placeholder="Reference, title, policy..." /></span></label>
        <label><span>Department</span><Select v-model="department" :options="departmentOptions" option-label="label" option-value="value" :loading="optionsLoading" @change="loadCirculars(true)" /></label>
        <label><span>Tag</span><Select v-model="tag" :options="tagOptions" option-label="label" option-value="value" :loading="optionsLoading" @change="loadCirculars(true)" /></label>
        <div class="year-fields">
          <label><span>From year</span><InputNumber v-model="startYear" :use-grouping="false" :min="1900" :max="2100" placeholder="From" /></label>
          <label><span>To year</span><InputNumber v-model="endYear" :use-grouping="false" :min="1900" :max="2100" placeholder="To" /></label>
        </div>
        <label><span>Sort</span><Select v-model="sortBy" :options="sortOptions" option-label="label" option-value="value" @change="loadCirculars(true)" /></label>
        <Button type="submit" label="Search" icon="pi pi-search" :loading="loading" />
      </form>

      <div class="filter-utilities">
        <Button label="Export CSV" icon="pi pi-download" size="small" text :loading="exportLoading" @click="exportCsv" />
        <Button label="Download ZIP" icon="pi pi-file-zip" size="small" text :disabled="!selectedIds.length" :loading="zipLoading" @click="downloadSelectedZip" />
        <Button label="Chat" icon="pi pi-comments" size="small" text :disabled="!selectedIds.length" @click="handoffToChat" />
      </div>
    </aside>

    <main class="circular-results-pane">
      <div class="results-toolbar">
        <div class="results-count"><strong>{{ totalRecords.toLocaleString() }}</strong><span> results</span></div>
        <div class="results-toolbar-actions">
          <Checkbox :model-value="allPageSelected" binary aria-label="Select page" @update:model-value="togglePageSelection" />
          <span v-if="selectedIds.length">{{ selectedIds.length }} selected</span>
          <Select v-model="perPage" :options="pageSizeOptions" option-label="label" option-value="value" size="small" />
        </div>
      </div>

      <Message v-if="errorMessage" severity="error" :closable="false">{{ errorMessage }}</Message>
      <div class="circular-result-list" :class="{ loading }">
        <button
          v-for="row in rows"
          :key="row.id"
          type="button"
          class="circular-result-item"
          :class="{ active: row.id === selectedCircularId }"
          @click="openCircular(row.id)"
        >
          <span class="result-select" @click.stop>
            <Checkbox v-model="selectedIds" :value="row.id" :input-id="`select-${row.id}`" :aria-label="`Select ${row.title}`" />
          </span>
          <span class="result-content">
            <span class="result-topline">
              <code>{{ row.reference || 'No reference' }}</code>
              <Tag :value="row.status" :severity="statusSeverity(row.status)" />
            </span>
            <strong>{{ row.title }}</strong>
            <span class="result-meta">{{ row.department || 'Unassigned' }} · {{ formatDate(row.date) }}</span>
            <span v-if="!selectedCircularId && row.snippet" class="result-snippet" v-html="row.snippet" />
            <span v-else-if="!selectedCircularId && row.summary" class="result-snippet">{{ row.summary }}</span>
            <span v-if="row.tags?.length" class="result-tags">
              <Tag v-for="item in row.tags.slice(0, selectedCircularId ? 1 : 3)" :key="item" :value="item" severity="secondary" />
            </span>
          </span>
        </button>
        <div v-if="!loading && !rows.length" class="empty-table-state"><i class="pi pi-search" /><strong>No circulars found</strong><span>Adjust the filters and try again.</span></div>
      </div>

      <div class="results-pagination">
        <Button icon="pi pi-angle-left" text rounded aria-label="Previous page" :disabled="page <= 1 || loading" @click="changePage(-1)" />
        <span>Page {{ page }} of {{ totalPages }}</span>
        <Button icon="pi pi-angle-right" text rounded aria-label="Next page" :disabled="page >= totalPages || loading" @click="changePage(1)" />
      </div>
    </main>

    <CircularDetailPane v-if="selectedCircularId" :id="selectedCircularId" @close="closeCircular" />
  </section>
</template>
