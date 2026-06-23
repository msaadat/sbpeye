<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Select from 'primevue/select'
import CircularDetailPane from '@/components/CircularDetailPane.vue'
import CircularResultContent from '@/components/CircularResultContent.vue'
import {
  createResearchWorkspace,
  deleteResearchWorkspace,
  downloadBatchZip,
  downloadCsvExport,
  getCircularDepartments,
  getCircularSearch,
  getCircularTags,
  getResearchWorkspace,
  getResearchWorkspaces,
  pinWorkspaceCircular,
  updateResearchWorkspace,
  unpinWorkspaceCircular,
  type CircularSummary,
  type DepartmentCount,
  type ResearchWorkspace,
  type SearchFilters,
  type TagCount,
} from '@/lib/api'

interface SelectOption<T = string> { label: string; value: T }

const route = useRoute()
const router = useRouter()
const confirm = useConfirm()
const toast = useToast()

const rows = ref<CircularSummary[]>([])
const selectedIds = ref<string[]>([])
const workspaces = ref<ResearchWorkspace[]>([])
const activeWorkspaceId = ref('')
const newWorkspaceName = ref('')
const departments = ref<DepartmentCount[]>([])
const tags = ref<TagCount[]>([])
const loading = ref(false)
const workspacesLoading = ref(false)
const workspaceSaving = ref(false)
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
const activeWorkspace = computed(() => workspaces.value.find((workspace) => workspace.id === activeWorkspaceId.value) || null)
const pinnedCirculars = computed(() => activeWorkspace.value?.pinned_circulars || [])
const pinnedIds = computed(() => new Set(activeWorkspace.value?.pinned_circular_ids || []))

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
const workspaceOptions = computed<SelectOption[]>(() => [
  { label: 'No workspace', value: '' },
  ...workspaces.value.map((workspace) => ({
    label: `${workspace.name} (${workspace.pinned_count.toLocaleString()})`,
    value: workspace.id,
  })),
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

function routeHasSearchState(): boolean {
  return Boolean(
    queryString(route.query.q)
    || queryString(route.query.department)
    || queryString(route.query.tag)
    || queryString(route.query.start_year)
    || queryString(route.query.end_year)
    || queryString(route.query.sort_by)
    || queryString(route.query.page)
    || queryString(route.query.per_page),
  )
}

function applySearchState(state: SearchFilters = {}) {
  query.value = typeof state.q === 'string' ? state.q : ''
  department.value = typeof state.department === 'string' && state.department ? state.department : null
  tag.value = typeof state.tag === 'string' && state.tag ? state.tag : null
  startYear.value = Number(state.start_year) || null
  endYear.value = Number(state.end_year) || null
  sortBy.value = typeof state.sort_by === 'string' && state.sort_by ? state.sort_by : 'relevance'
  page.value = Math.max(1, Number(state.page) || 1)
  perPage.value = [20, 50, 100].includes(Number(state.per_page)) ? Number(state.per_page) : 20
}

function routeQuery(): Record<string, string> {
  const result: Record<string, string> = {}
  if (activeWorkspaceId.value) result.workspace = activeWorkspaceId.value
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

function upsertWorkspace(workspace: ResearchWorkspace) {
  const index = workspaces.value.findIndex((item) => item.id === workspace.id)
  if (index >= 0) {
    workspaces.value = [
      ...workspaces.value.slice(0, index),
      workspace,
      ...workspaces.value.slice(index + 1),
    ]
  } else {
    workspaces.value = [workspace, ...workspaces.value]
  }
}

async function saveActiveWorkspaceState(overrides: {
  name?: string
  search_state?: SearchFilters
  last_circular_id?: string | null
} = {}) {
  if (!activeWorkspaceId.value) return
  try {
    const workspace = await updateResearchWorkspace(activeWorkspaceId.value, {
      search_state: filters.value,
      last_circular_id: selectedCircularId.value || null,
      ...overrides,
    })
    upsertWorkspace(workspace)
  } catch (error) {
    toast.add({ severity: 'warn', summary: 'Workspace not saved', detail: error instanceof Error ? error.message : 'Unable to save workspace state.', life: 4000 })
  }
}

async function activateWorkspace(workspaceId: string, restoreState = true) {
  if (!workspaceId) {
    activeWorkspaceId.value = ''
    localStorage.removeItem('sbpeye-active-workspace')
    await syncRoute()
    return
  }

  workspacesLoading.value = true
  try {
    const workspace = await getResearchWorkspace(workspaceId)
    upsertWorkspace(workspace)
    activeWorkspaceId.value = workspace.id
    localStorage.setItem('sbpeye-active-workspace', workspace.id)
    if (restoreState) {
      applySearchState(workspace.search_state)
      await router.replace({
        path: workspace.last_circular_id ? `/circulars/${workspace.last_circular_id}` : '/circulars',
        query: routeQuery(),
      })
    } else {
      await syncRoute()
    }
  } catch (error) {
    activeWorkspaceId.value = ''
    localStorage.removeItem('sbpeye-active-workspace')
    toast.add({ severity: 'error', summary: 'Workspace unavailable', detail: error instanceof Error ? error.message : 'Unable to open workspace.', life: 6000 })
  } finally {
    workspacesLoading.value = false
  }
}

async function loadWorkspaces() {
  workspacesLoading.value = true
  try {
    workspaces.value = await getResearchWorkspaces()
    const requestedWorkspaceId = queryString(route.query.workspace)
    const storedWorkspaceId = localStorage.getItem('sbpeye-active-workspace') || ''
    const workspaceId = requestedWorkspaceId || (!routeHasSearchState() ? storedWorkspaceId : '')
    if (workspaceId && workspaces.value.some((workspace) => workspace.id === workspaceId)) {
      await activateWorkspace(workspaceId, true)
    }
  } catch (error) {
    toast.add({ severity: 'warn', summary: 'Workspaces unavailable', detail: error instanceof Error ? error.message : 'Unable to load research workspaces.', life: 5000 })
  } finally {
    workspacesLoading.value = false
  }
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
    await saveActiveWorkspaceState()
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

async function createWorkspace() {
  const name = newWorkspaceName.value.trim()
  if (!name || workspaceSaving.value) return
  workspaceSaving.value = true
  try {
    let workspace = await createResearchWorkspace({
      name,
      search_state: filters.value,
      last_circular_id: selectedCircularId.value || null,
    })
    upsertWorkspace(workspace)
    activeWorkspaceId.value = workspace.id
    localStorage.setItem('sbpeye-active-workspace', workspace.id)
    newWorkspaceName.value = ''

    for (const circularId of selectedIds.value) {
      workspace = await pinWorkspaceCircular(workspace.id, circularId)
    }
    upsertWorkspace(workspace)
    await syncRoute()
    toast.add({ severity: 'success', summary: 'Workspace created', life: 2200 })
  } catch (error) {
    toast.add({ severity: 'error', summary: 'Workspace not created', detail: error instanceof Error ? error.message : 'Unable to create workspace.', life: 6000 })
  } finally {
    workspaceSaving.value = false
  }
}

async function handleWorkspaceChange() {
  await activateWorkspace(activeWorkspaceId.value, true)
  await loadCirculars()
}

function confirmDeleteWorkspace() {
  if (!activeWorkspace.value || workspaceSaving.value) return
  const workspace = activeWorkspace.value
  confirm.require({
    message: `Delete "${workspace.name}"? Pinned circulars stay in the SBPEye index.`,
    header: 'Delete workspace',
    icon: 'pi pi-trash',
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    acceptProps: { label: 'Delete', severity: 'danger' },
    accept: () => { void removeWorkspace(workspace.id) },
  })
}

async function removeWorkspace(workspaceId: string) {
  workspaceSaving.value = true
  try {
    await deleteResearchWorkspace(workspaceId)
    workspaces.value = workspaces.value.filter((workspace) => workspace.id !== workspaceId)
    if (activeWorkspaceId.value === workspaceId) {
      activeWorkspaceId.value = ''
      localStorage.removeItem('sbpeye-active-workspace')
      await syncRoute()
    }
    toast.add({ severity: 'success', summary: 'Workspace deleted', life: 2200 })
  } catch (error) {
    toast.add({ severity: 'error', summary: 'Delete failed', detail: error instanceof Error ? error.message : 'Unable to delete workspace.', life: 6000 })
  } finally {
    workspaceSaving.value = false
  }
}

function isPinned(id: string): boolean {
  return pinnedIds.value.has(id)
}

async function togglePinned(id: string) {
  if (!activeWorkspaceId.value || workspaceSaving.value) return
  workspaceSaving.value = true
  try {
    const workspace = isPinned(id)
      ? await unpinWorkspaceCircular(activeWorkspaceId.value, id)
      : await pinWorkspaceCircular(activeWorkspaceId.value, id)
    upsertWorkspace(workspace)
  } catch (error) {
    toast.add({ severity: 'error', summary: 'Workspace update failed', detail: error instanceof Error ? error.message : 'Unable to update pinned circulars.', life: 6000 })
  } finally {
    workspaceSaving.value = false
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

async function openCircular(id: string) {
  await router.push({ path: `/circulars/${id}`, query: routeQuery() })
  await saveActiveWorkspaceState({ last_circular_id: id })
}

function closeCircular() {
  void router.push({ path: '/circulars', query: routeQuery() })
  void saveActiveWorkspaceState({ last_circular_id: null })
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

function handoffWorkspaceToChat() {
  const circularIds = activeWorkspace.value?.pinned_circular_ids || []
  if (circularIds.length) void router.push({ path: '/chat', query: { circular_ids: circularIds.join(',') } })
}

function changePage(offset: number) {
  page.value = Math.min(totalPages.value, Math.max(1, page.value + offset))
  void loadCirculars()
}

watch(perPage, () => { if (!loading.value) void loadCirculars(true) })

onMounted(async () => {
  readRouteFilters()
  void loadOptions()
  await loadWorkspaces()
  void loadCirculars()
})

onBeforeUnmount(() => searchController?.abort())
</script>

<template>
  <section class="circular-workspace" :class="{ 'has-detail': selectedCircularId }">
    <aside class="circular-filters">
      <div class="workspace-brandline">
        <div><span>Circulars</span><strong>Search workspace</strong></div>
        <Button icon="pi pi-filter-slash" size="small" text rounded aria-label="Reset filters" :disabled="!hasFilters" @click="clearFilters" />
      </div>

      <section class="research-workspace-panel">
        <div class="workspace-panel-heading">
          <span>Research workspace</span>
          <Button
            icon="pi pi-trash"
            size="small"
            text
            rounded
            severity="danger"
            aria-label="Delete workspace"
            :disabled="!activeWorkspace || workspaceSaving"
            @click="confirmDeleteWorkspace"
          />
        </div>
        <Select
          v-model="activeWorkspaceId"
          :options="workspaceOptions"
          option-label="label"
          option-value="value"
          size="small"
          :loading="workspacesLoading"
          @change="handleWorkspaceChange"
        />
        <form class="workspace-create-form" @submit.prevent="createWorkspace">
          <InputText v-model="newWorkspaceName" size="small" maxlength="120" placeholder="New topic name" />
          <Button icon="pi pi-plus" type="submit" size="small" text rounded aria-label="Create workspace" :disabled="!newWorkspaceName.trim()" :loading="workspaceSaving" />
        </form>
        <div v-if="activeWorkspace" class="workspace-pinned-block">
          <div class="workspace-pinned-heading">
            <span>{{ pinnedCirculars.length }} pinned</span>
            <Button label="Chat" icon="pi pi-comments" size="small" text :disabled="!pinnedCirculars.length" @click="handoffWorkspaceToChat" />
          </div>
          <div v-if="pinnedCirculars.length" class="workspace-pinned-list">
            <div
              v-for="circular in pinnedCirculars"
              :key="circular.id"
              class="workspace-pinned-item"
              :class="{ active: circular.id === selectedCircularId }"
            >
              <button type="button" @click="openCircular(circular.id)">
                <span>{{ circular.reference || circular.title }}</span>
              </button>
              <Button icon="pi pi-times" text rounded size="small" severity="secondary" :aria-label="`Unpin ${circular.title}`" @click.stop="togglePinned(circular.id)" />
            </div>
          </div>
        </div>
      </section>

      <form class="filter-stack" @submit.prevent="loadCirculars(true)">
        <label><span>Search</span><span class="filter-search"><i class="pi pi-search" /><InputText v-model="query" size="small" placeholder="Reference, title, policy..." /></span></label>
        <label><span>Department</span><Select v-model="department" :options="departmentOptions" option-label="label" option-value="value" size="small" :loading="optionsLoading" @change="loadCirculars(true)" /></label>
        <label><span>Tag</span><Select v-model="tag" :options="tagOptions" option-label="label" option-value="value" size="small" :loading="optionsLoading" @change="loadCirculars(true)" /></label>
        <div class="year-fields">
          <label><span>From year</span><InputNumber v-model="startYear" size="small" :use-grouping="false" :min="1900" :max="2100" placeholder="From" /></label>
          <label><span>To year</span><InputNumber v-model="endYear" size="small" :use-grouping="false" :min="1900" :max="2100" placeholder="To" /></label>
        </div>
        <label><span>Sort</span><Select v-model="sortBy" :options="sortOptions" option-label="label" option-value="value" size="small" @change="loadCirculars(true)" /></label>
        <Button type="submit" label="Search" icon="pi pi-search" size="small" :loading="loading" />
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
            <Button
              :icon="isPinned(row.id) ? 'pi pi-star-fill' : 'pi pi-star'"
              text
              rounded
              size="small"
              severity="secondary"
              :disabled="!activeWorkspace"
              :aria-label="isPinned(row.id) ? `Unpin ${row.title}` : `Pin ${row.title}`"
              @click="togglePinned(row.id)"
            />
          </span>
          <CircularResultContent
            :circular="row"
            :show-snippet="!selectedCircularId"
            :max-tags="selectedCircularId ? 1 : 3"
          />
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
