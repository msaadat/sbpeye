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
import { useResizablePane } from '@/lib/useResizablePane'
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
const selectionMode = ref(false)
const workspaces = ref<ResearchWorkspace[]>([])
const activeWorkspaceId = ref('')
const newWorkspaceName = ref('')
const creatingWorkspace = ref(false)
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
const resultsPane = useResizablePane('sbp:resultsPaneWidth', 320, 224, 640)
const totalPages = computed(() => Math.max(1, Math.ceil(totalRecords.value / perPage.value)))
const hasFilters = computed(() => Boolean(query.value.trim() || department.value || tag.value || startYear.value || endYear.value))
const activeWorkspace = computed(() => workspaces.value.find((workspace) => workspace.id === activeWorkspaceId.value) || null)
const defaultWorkspace = computed(() => workspaces.value.find((workspace) => workspace.is_default) || workspaces.value[0] || null)
const pinnedCirculars = computed(() => activeWorkspace.value?.pinned_circulars || [])
const pinnedIds = computed(() => new Set(activeWorkspace.value?.pinned_circular_ids || []))
const searchRows = computed(() => rows.value.filter((row) => !pinnedIds.value.has(row.id)))
const allPageSelected = computed(() => Boolean(searchRows.value.length) && searchRows.value.every((row) => selectedIds.value.includes(row.id)))
const canDeleteActiveWorkspace = computed(() => Boolean(activeWorkspace.value && !activeWorkspace.value.is_default))

const sortOptions: SelectOption[] = [
  { label: 'Relevance', value: 'relevance' },
  { label: 'Newest first', value: 'date' },
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
  if (activeWorkspaceId.value && !activeWorkspace.value?.is_default) result.workspace = activeWorkspaceId.value
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

function orderWorkspaces(items: ResearchWorkspace[]): ResearchWorkspace[] {
  return [...items].sort((a, b) => {
    const defaultDelta = Number(Boolean(b.is_default)) - Number(Boolean(a.is_default))
    if (defaultDelta !== 0) return defaultDelta
    const createdA = Date.parse(a.created_at || '') || 0
    const createdB = Date.parse(b.created_at || '') || 0
    if (createdA !== createdB) return createdA - createdB
    return a.id.localeCompare(b.id)
  })
}

function upsertWorkspace(workspace: ResearchWorkspace) {
  const index = workspaces.value.findIndex((item) => item.id === workspace.id)
  if (index >= 0) {
    workspaces.value = orderWorkspaces([
      ...workspaces.value.slice(0, index),
      workspace,
      ...workspaces.value.slice(index + 1),
    ])
  } else {
    workspaces.value = orderWorkspaces([workspace, ...workspaces.value])
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
    workspaceId = defaultWorkspace.value?.id || ''
    if (!workspaceId) return
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
    activeWorkspaceId.value = defaultWorkspace.value?.id || ''
    localStorage.removeItem('sbpeye-active-workspace')
    toast.add({ severity: 'error', summary: 'Workspace unavailable', detail: error instanceof Error ? error.message : 'Unable to open workspace.', life: 6000 })
  } finally {
    workspacesLoading.value = false
  }
}

async function loadWorkspaces() {
  workspacesLoading.value = true
  try {
    workspaces.value = orderWorkspaces(await getResearchWorkspaces())
    const requestedWorkspaceId = queryString(route.query.workspace)
    const storedWorkspaceId = localStorage.getItem('sbpeye-active-workspace') || ''
    const defaultId = defaultWorkspace.value?.id || ''
    const requestedExists = requestedWorkspaceId && workspaces.value.some((workspace) => workspace.id === requestedWorkspaceId)
    const storedExists = storedWorkspaceId && workspaces.value.some((workspace) => workspace.id === storedWorkspaceId)
    const shouldRestoreWorkspaceState = !routeHasSearchState() && !selectedCircularId.value
    const workspaceId = requestedExists
      ? requestedWorkspaceId
      : (shouldRestoreWorkspaceState && storedExists ? storedWorkspaceId : defaultId)
    if (workspaceId) await activateWorkspace(workspaceId, shouldRestoreWorkspaceState)
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
    creatingWorkspace.value = false

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

function beginCreateWorkspace() {
  creatingWorkspace.value = true
}

function cancelCreateWorkspace() {
  creatingWorkspace.value = false
  newWorkspaceName.value = ''
}

async function openWorkspaceTab(workspaceId: string) {
  if (workspaceId === activeWorkspaceId.value || workspacesLoading.value) return
  await activateWorkspace(workspaceId, true)
  await loadCirculars()
}

function confirmDeleteWorkspace() {
  const workspace = activeWorkspace.value
  if (!workspace || workspace.is_default || workspaceSaving.value) return
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
      const fallbackId = defaultWorkspace.value?.id || ''
      activeWorkspaceId.value = fallbackId
      if (fallbackId) {
        localStorage.setItem('sbpeye-active-workspace', fallbackId)
        await activateWorkspace(fallbackId, true)
        await loadCirculars()
      } else {
        localStorage.removeItem('sbpeye-active-workspace')
        await syncRoute()
      }
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
  if (!activeWorkspaceId.value && defaultWorkspace.value) activeWorkspaceId.value = defaultWorkspace.value.id
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
    const pageIds = new Set(searchRows.value.map((row) => row.id))
    selectedIds.value = selectedIds.value.filter((id) => !pageIds.has(id))
  } else {
    selectedIds.value = [...new Set([...selectedIds.value, ...searchRows.value.map((row) => row.id)])]
  }
}

function toggleSelectionMode() {
  selectionMode.value = !selectionMode.value
  if (!selectionMode.value) selectedIds.value = []
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
  if (activeWorkspace.value) void router.push({ path: '/chat', query: { workspace: activeWorkspace.value.id } })
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
  <section class="circulars-screen">
    <header class="workspace-tabs-bar">
      <div class="workspace-tabs" role="tablist" aria-label="Research workspaces">
        <button
          v-for="workspace in workspaces"
          :key="workspace.id"
          type="button"
          class="workspace-tab"
          :class="{ active: workspace.id === activeWorkspaceId }"
          role="tab"
          :aria-selected="workspace.id === activeWorkspaceId"
          @click="openWorkspaceTab(workspace.id)"
        >
          <i class="pi" :class="workspace.is_default ? 'pi-home' : 'pi-folder'" />
          <span class="workspace-tab-label">{{ workspace.name }}</span>
          <span class="workspace-tab-count">{{ workspace.pinned_count.toLocaleString() }}</span>
        </button>

        <form v-if="creatingWorkspace" class="workspace-create-form workspace-create-tab" @submit.prevent="createWorkspace">
          <InputText v-model="newWorkspaceName" size="small" maxlength="120" placeholder="Workspace name" autofocus />
          <Button icon="pi pi-check" type="submit" size="small" text rounded aria-label="Create workspace" :disabled="!newWorkspaceName.trim()" :loading="workspaceSaving" />
          <Button icon="pi pi-times" type="button" size="small" text rounded severity="secondary" aria-label="Cancel workspace creation" @click="cancelCreateWorkspace" />
        </form>

        <Button v-else icon="pi pi-plus" size="small" text rounded aria-label="Create workspace" @click="beginCreateWorkspace" />
      </div>

      <div class="workspace-actions">
        <span class="workspace-pinned-summary"><i class="pi pi-bookmark-fill" />{{ pinnedCirculars.length.toLocaleString() }} pinned</span>
        <Button label="Chat" icon="pi pi-comments" size="small" text :disabled="!activeWorkspace" @click="handoffWorkspaceToChat" />
        <Button
          icon="pi pi-trash"
          size="small"
          text
          rounded
          severity="danger"
          aria-label="Delete workspace"
          :disabled="!canDeleteActiveWorkspace || workspaceSaving"
          @click="confirmDeleteWorkspace"
        />
      </div>
    </header>

    <section class="search-controls-bar" aria-label="Search controls">
      <form class="search-controls-form" @submit.prevent="loadCirculars(true)">
        <span class="search-input-wrap">
          <i class="pi pi-search search-icon" />
          <InputText
            v-model="query"
            size="small"
            placeholder="Reference, title, policy..."
            class="search-main-input"
          />
          <Button
            v-if="query"
            icon="pi pi-times"
            text
            rounded
            size="small"
            aria-label="Clear search"
            title="Clear search"
            type="button"
            class="search-clear-btn"
            @click="query = ''; loadCirculars(true)"
          />
        </span>

        <Select
          v-model="department"
          :options="departmentOptions"
          option-label="label"
          option-value="value"
          size="small"
          placeholder="Department"
          :loading="optionsLoading"
          class="search-select"
          @change="loadCirculars(true)"
        />

        <Select
          v-model="tag"
          :options="tagOptions"
          option-label="label"
          option-value="value"
          size="small"
          placeholder="Tag"
          :loading="optionsLoading"
          class="search-select search-select-sm"
          @change="loadCirculars(true)"
        />

        <span class="year-range">
          <InputNumber
            v-model="startYear"
            size="small"
            :use-grouping="false"
            :min="1900"
            :max="2100"
            placeholder="From"
            class="year-input"
          />
          <span class="year-sep">–</span>
          <InputNumber
            v-model="endYear"
            size="small"
            :use-grouping="false"
            :min="1900"
            :max="2100"
            placeholder="To"
            class="year-input"
          />
        </span>

        <Select
          v-model="sortBy"
          :options="sortOptions"
          option-label="label"
          option-value="value"
          size="small"
          class="search-select"
          @change="loadCirculars(true)"
        />

        <Button
          type="submit"
          icon="pi pi-search"
          size="small"
          :loading="loading"
          aria-label="Search"
          title="Search"
        />

        <Button
          v-if="hasFilters"
          icon="pi pi-filter-slash"
          size="small"
          text
          rounded
          aria-label="Clear all filters"
          title="Clear all filters"
          type="button"
          @click="clearFilters"
        />
      </form>

      <div class="search-utilities">
        <Button
          :label="selectionMode ? 'Done' : 'Select'"
          icon="pi pi-check-square"
          size="small"
          text
          :title="selectionMode ? 'Exit selection mode' : 'Select circulars'"
          @click="toggleSelectionMode"
        />
        <Button label="CSV" icon="pi pi-download" size="small" text title="Export CSV" :loading="exportLoading" @click="exportCsv" />
        <Button label="ZIP" icon="pi pi-file-zip" size="small" text title="Download selected as ZIP" :disabled="!selectedIds.length" :loading="zipLoading" @click="downloadSelectedZip" />
        <Button label="Chat" icon="pi pi-comments" size="small" text title="Open selected in Chat" :disabled="!selectedIds.length" @click="handoffToChat" />
      </div>
    </section>

    <section
      class="circular-workspace"
      :class="{ 'has-detail': selectedCircularId }"
      :style="{ '--results-pane-width': `${resultsPane.size.value}px` }"
    >
      <main class="circular-results-pane glass-panel">
        <Message v-if="errorMessage" severity="error" :closable="false">{{ errorMessage }}</Message>
        <div class="circular-result-list" :class="{ loading }">
          <section v-if="pinnedCirculars.length" class="pinned-results-section" aria-label="Pinned circulars">

            <button
              v-for="circular in pinnedCirculars"
              :key="circular.id"
              type="button"
              class="circular-result-item pinned-result-item"
              :class="{ active: circular.id === selectedCircularId }"
              @click="openCircular(circular.id)"
            >
              <span class="result-select" @click.stop>
                <Checkbox v-if="selectionMode" v-model="selectedIds" :value="circular.id" :input-id="`select-pinned-${circular.id}`" :aria-label="`Select ${circular.title}`" />
                <Button
                  icon="pi pi-bookmark-fill"
                  text
                  rounded
                  size="small"
                  severity="secondary"
                  :disabled="workspaceSaving"
                  :aria-label="`Unpin ${circular.title}`"
                  @click="togglePinned(circular.id)"
                />
              </span>
              <CircularResultContent
                :circular="circular"
                :show-snippet="true"
                :max-tags="selectedCircularId ? 1 : 3"
              />
            </button>
          </section>

          <button
            v-for="row in searchRows"
            :key="row.id"
            type="button"
            class="circular-result-item"
            :class="{ active: row.id === selectedCircularId }"
            @click="openCircular(row.id)"
          >
            <span v-if="selectionMode" class="result-select" @click.stop>
              <Checkbox v-model="selectedIds" :value="row.id" :input-id="`select-${row.id}`" :aria-label="`Select ${row.title}`" />
            </span>
            <CircularResultContent
              :circular="row"
              :show-snippet="true"
              :max-tags="selectedCircularId ? 1 : 3"
            />
          </button>
          <div v-if="!loading && !pinnedCirculars.length && !searchRows.length" class="empty-table-state"><i class="pi pi-search" /><strong>No circulars found</strong><span>Adjust the filters and try again.</span></div>
        </div>

        <div class="results-toolbar">
          <div class="results-count"><strong>{{ totalRecords.toLocaleString() }}</strong><span> results</span></div>
          <div class="results-toolbar-actions">
            <Checkbox v-if="selectionMode" :model-value="allPageSelected" binary aria-label="Select page" :disabled="!searchRows.length" @update:model-value="togglePageSelection" />
            <span v-if="selectionMode && selectedIds.length">{{ selectedIds.length }} selected</span>
            <div class="results-pagination">
              <Button icon="pi pi-angle-left" text rounded aria-label="Previous page" :disabled="page <= 1 || loading" @click="changePage(-1)" />
              <span>Page {{ page }} of {{ totalPages }}</span>
              <Button icon="pi pi-angle-right" text rounded aria-label="Next page" :disabled="page >= totalPages || loading" @click="changePage(1)" />
            </div>
          </div>
        </div>
      </main>

      <div
        v-if="selectedCircularId"
        class="pane-resizer"
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize results panel"
        :class="{ resizing: resultsPane.resizing.value }"
        @pointerdown="resultsPane.startDrag"
        @dblclick="resultsPane.resetToDefault"
      />

      <CircularDetailPane
        v-if="selectedCircularId"
        :id="selectedCircularId"
        :is-pinned="isPinned(selectedCircularId)"
        :pin-pending="workspaceSaving"
        @close="closeCircular"
        @toggle-pin="togglePinned(selectedCircularId)"
      />
    </section>
  </section>
</template>
