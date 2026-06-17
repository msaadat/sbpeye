<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Column from 'primevue/column'
import DataTable from 'primevue/datatable'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
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

const PdfPreviewDialog = defineAsyncComponent(() => import('@/components/PdfPreviewDialog.vue'))

interface SelectOption<T = string> {
  label: string
  value: T
}

interface DataTablePageEvent {
  first: number
  rows: number
  page: number
}

const router = useRouter()
const toast = useToast()

const rows = ref<CircularSummary[]>([])
const selectedRows = ref<CircularSummary[]>([])
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

const pdfDialogVisible = ref(false)
const pdfTitle = ref('')
const pdfUrl = ref('')

const sortOptions: SelectOption[] = [
  { label: 'Relevance', value: 'relevance' },
  { label: 'Newest first', value: 'date' },
]

const departmentOptions = computed<SelectOption[]>(() => [
  { label: 'All departments', value: '' },
  ...departments.value
    .filter((item) => item.department)
    .map((item) => ({
      label: `${item.department} (${item.count.toLocaleString()})`,
      value: item.department || '',
    })),
])

const tagOptions = computed<SelectOption[]>(() => [
  { label: 'All tags', value: '' },
  ...tags.value.map((item) => ({
    label: `${item.tag} (${item.count.toLocaleString()})`,
    value: item.tag,
  })),
])

const filters = computed<SearchFilters>(() => ({
  q: query.value.trim(),
  start_year: startYear.value,
  end_year: endYear.value,
  department: department.value || null,
  sort_by: sortBy.value,
  tag: tag.value || null,
  page: page.value,
  per_page: perPage.value,
}))

const selectedCountLabel = computed(() => {
  const count = selectedRows.value.length
  return count === 1 ? '1 selected' : `${count} selected`
})

const hasFilters = computed(() =>
  Boolean(query.value.trim() || department.value || tag.value || startYear.value || endYear.value),
)

function isPdfUrl(url?: string | null): boolean {
  if (!url) {
    return false
  }

  return url.toLowerCase().split('?', 1)[0].endsWith('.pdf')
}

function formatDate(value?: string | null): string {
  if (!value) {
    return 'Not dated'
  }

  return new Intl.DateTimeFormat(undefined, {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
  }).format(new Date(value))
}

function statusSeverity(status: string): 'success' | 'info' | 'warn' | 'danger' | 'secondary' | 'contrast' {
  const normalized = status.toLowerCase()

  if (normalized.includes('active') || normalized.includes('indexed')) {
    return 'success'
  }

  if (normalized.includes('superseded') || normalized.includes('replaced')) {
    return 'warn'
  }

  if (normalized.includes('withdrawn') || normalized.includes('cancel')) {
    return 'danger'
  }

  return 'info'
}

async function loadOptions() {
  optionsLoading.value = true

  try {
    const [departmentItems, tagItems] = await Promise.all([
      getCircularDepartments(),
      getCircularTags(),
    ])
    departments.value = departmentItems
    tags.value = tagItems
  } catch (error) {
    toast.add({
      severity: 'warn',
      summary: 'Filter options unavailable',
      detail: error instanceof Error ? error.message : 'Unable to load departments or tags.',
      life: 5000,
    })
  } finally {
    optionsLoading.value = false
  }
}

async function loadCirculars(resetPage = false) {
  if (resetPage) {
    page.value = 1
  }

  loading.value = true
  errorMessage.value = ''

  try {
    const response = await getCircularSearch(filters.value)
    rows.value = response.items
    totalRecords.value = response.total
    page.value = response.page
    perPage.value = response.per_page
  } catch (error) {
    rows.value = []
    totalRecords.value = 0
    errorMessage.value = error instanceof Error ? error.message : 'Unable to load circulars.'
  } finally {
    loading.value = false
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

function onPage(event: DataTablePageEvent) {
  page.value = event.page + 1
  perPage.value = event.rows
  void loadCirculars()
}

async function exportCsv() {
  exportLoading.value = true

  try {
    await downloadCsvExport(filters.value)
    toast.add({
      severity: 'success',
      summary: 'CSV export started',
      detail: 'The current filter set is being downloaded.',
      life: 3500,
    })
  } catch (error) {
    toast.add({
      severity: 'error',
      summary: 'CSV export failed',
      detail: error instanceof Error ? error.message : 'Unable to export CSV.',
      life: 6000,
    })
  } finally {
    exportLoading.value = false
  }
}

async function downloadSelectedZip() {
  const ids = selectedRows.value.map((item) => item.id)
  if (!ids.length) {
    return
  }

  zipLoading.value = true

  try {
    await downloadBatchZip(ids)
    toast.add({
      severity: 'success',
      summary: 'ZIP download started',
      detail: `${ids.length.toLocaleString()} circulars requested.`,
      life: 3500,
    })
  } catch (error) {
    toast.add({
      severity: 'error',
      summary: 'ZIP download failed',
      detail: error instanceof Error ? error.message : 'Unable to download selected circulars.',
      life: 6000,
    })
  } finally {
    zipLoading.value = false
  }
}

function handoffToChat() {
  const ids = selectedRows.value.map((item) => item.id)
  if (!ids.length) {
    return
  }

  void router.push({
    path: '/chat',
    query: {
      circular_ids: ids.join(','),
    },
  })
}

function openPdfPreview(row: CircularSummary) {
  if (!row.url) {
    return
  }

  pdfTitle.value = row.title
  pdfUrl.value = row.url
  pdfDialogVisible.value = true
}

onMounted(() => {
  void loadOptions()
  void loadCirculars()
})
</script>

<template>
  <section class="view-stack">
    <div class="page-heading">
      <div>
        <p>Circulars</p>
        <h1>Search and browse circulars</h1>
      </div>
      <div class="button-row">
        <Button
          label="Export CSV"
          icon="pi pi-download"
          :loading="exportLoading"
          @click="exportCsv"
        />
        <Button
          label="Download ZIP"
          icon="pi pi-file-zip"
          severity="secondary"
          :disabled="!selectedRows.length"
          :loading="zipLoading"
          @click="downloadSelectedZip"
        />
        <Button
          label="Chat with selection"
          icon="pi pi-comments"
          severity="contrast"
          :disabled="!selectedRows.length"
          @click="handoffToChat"
        />
      </div>
    </div>

    <Card>
      <template #content>
        <div class="filter-grid circular-filter-grid">
          <span class="p-input-icon-left circular-query">
            <i class="pi pi-search" />
            <InputText
              v-model="query"
              placeholder="Search circulars, references, departments, or policy topics"
              @keyup.enter="loadCirculars(true)"
            />
          </span>
          <Select
            v-model="department"
            :options="departmentOptions"
            option-label="label"
            option-value="value"
            placeholder="Department"
            show-clear
            :loading="optionsLoading"
            @change="loadCirculars(true)"
          />
          <Select
            v-model="tag"
            :options="tagOptions"
            option-label="label"
            option-value="value"
            placeholder="Tag"
            show-clear
            :loading="optionsLoading"
            @change="loadCirculars(true)"
          />
          <Select
            v-model="sortBy"
            :options="sortOptions"
            option-label="label"
            option-value="value"
            placeholder="Sort by"
            @change="loadCirculars(true)"
          />
          <InputNumber
            v-model="startYear"
            input-id="start-year"
            :use-grouping="false"
            :min="1900"
            :max="2100"
            placeholder="Start year"
            @keyup.enter="loadCirculars(true)"
          />
          <InputNumber
            v-model="endYear"
            input-id="end-year"
            :use-grouping="false"
            :min="1900"
            :max="2100"
            placeholder="End year"
            @keyup.enter="loadCirculars(true)"
          />
          <div class="filter-actions">
            <Button
              label="Search"
              icon="pi pi-search"
              :loading="loading"
              @click="loadCirculars(true)"
            />
            <Button
              label="Reset"
              icon="pi pi-filter-slash"
              severity="secondary"
              outlined
              :disabled="!hasFilters && sortBy === 'relevance'"
              @click="clearFilters"
            />
          </div>
        </div>
      </template>
    </Card>

    <Message v-if="errorMessage" severity="error" :closable="false">
      {{ errorMessage }}
    </Message>

    <div class="table-shell">
      <div class="table-summary">
        <span>
          {{ totalRecords.toLocaleString() }} results
          <template v-if="hasFilters">matching current filters</template>
        </span>
        <Tag :value="selectedCountLabel" severity="secondary" />
      </div>

      <DataTable
        v-model:selection="selectedRows"
        :value="rows"
        data-key="id"
        lazy
        paginator
        striped-rows
        removable-sort
        :loading="loading"
        :rows="perPage"
        :first="(page - 1) * perPage"
        :total-records="totalRecords"
        :rows-per-page-options="[10, 20, 50, 100]"
        table-style="min-width: 72rem"
        @page="onPage"
      >
        <template #empty>
          <div class="empty-table-state">
            <i class="pi pi-search" />
            <strong>No circulars found</strong>
            <span>Adjust filters or search terms to broaden the result set.</span>
          </div>
        </template>

        <Column selection-mode="multiple" header-style="width: 3rem" />

        <Column field="title" header="Circular" style="min-width: 24rem">
          <template #body="{ data }">
            <div class="circular-title-cell">
              <RouterLink :to="`/circulars/${data.id}`" class="record-title">
                {{ data.title }}
              </RouterLink>
              <div class="record-meta">
                <span>{{ data.reference || 'No reference' }}</span>
                <span>{{ formatDate(data.date) }}</span>
              </div>
              <p v-if="data.snippet" class="snippet" v-html="data.snippet" />
              <p v-else-if="data.summary" class="snippet">
                {{ data.summary }}
              </p>
            </div>
          </template>
        </Column>

        <Column field="department" header="Department" style="min-width: 14rem">
          <template #body="{ data }">
            <span>{{ data.department || 'Unassigned' }}</span>
          </template>
        </Column>

        <Column field="status" header="Status" style="min-width: 8rem">
          <template #body="{ data }">
            <Tag :value="data.status" :severity="statusSeverity(data.status)" />
          </template>
        </Column>

        <Column header="Tags" style="min-width: 14rem">
          <template #body="{ data }">
            <div v-if="data.tags?.length" class="tag-list">
              <Tag
                v-for="item in data.tags.slice(0, 3)"
                :key="item"
                :value="item"
                severity="secondary"
              />
              <Tag
                v-if="data.tags.length > 3"
                :value="`+${data.tags.length - 3}`"
                severity="secondary"
              />
            </div>
            <span v-else class="muted-text">No tags</span>
          </template>
        </Column>

        <Column header="Actions" frozen align-frozen="right" style="min-width: 12rem">
          <template #body="{ data }">
            <div class="row-actions">
              <Button
                as="router-link"
                :to="`/circulars/${data.id}`"
                icon="pi pi-eye"
                text
                rounded
                aria-label="Open detail"
              />
              <Button
                v-if="data.url"
                as="a"
                :href="data.url"
                target="_blank"
                rel="noreferrer"
                icon="pi pi-external-link"
                text
                rounded
                aria-label="Open original source"
              />
              <Button
                v-if="isPdfUrl(data.url)"
                icon="pi pi-file-pdf"
                text
                rounded
                severity="danger"
                aria-label="Preview PDF"
                @click="openPdfPreview(data)"
              />
            </div>
          </template>
        </Column>
      </DataTable>
    </div>

    <PdfPreviewDialog
      v-model:visible="pdfDialogVisible"
      :title="pdfTitle"
      :url="pdfUrl"
    />
  </section>
</template>
