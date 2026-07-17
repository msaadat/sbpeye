<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import Button from 'primevue/button'
import ConfirmDialog from 'primevue/confirmdialog'
import Message from 'primevue/message'
import Toast from 'primevue/toast'
import { useToast } from 'primevue/usetoast'
import SbpNewsPanel from '@/components/SbpNewsPanel.vue'
import { getAppStatus, getLlmStatus, startCircularSync, type ApiError, type AppStatus, type CircularSyncStatus, type LlmStatus } from '@/lib/api'

const route = useRoute()
const toast = useToast()
const darkMode = ref(localStorage.getItem('sbpeye-theme') === 'dark')
const status = ref<AppStatus | null>(null)
const statusLoading = ref(false)
const statusError = ref('')
const llmStatus = ref<LlmStatus | null>(null)
const llmLoading = ref(false)
const llmError = ref('')
const syncStarting = ref(false)
let statusPollId: ReturnType<typeof setInterval> | null = null

const navItems = computed(() => [
  {
    label: 'Circulars',
    icon: 'pi pi-table',
    route: '/circulars',
    active: route.path.startsWith('/circulars'),
  },
  {
    label: 'Chat',
    icon: 'pi pi-comments',
    route: '/chat',
    active: route.path.startsWith('/chat'),
  },
  {
    label: 'Values',
    icon: 'pi pi-percentage',
    route: '/values',
    active: route.path.startsWith('/values'),
  },
  {
    label: 'EcoData',
    icon: 'pi pi-chart-line',
    route: '/ecodata',
    active: route.path.startsWith('/ecodata'),
  },
  {
    label: 'Settings',
    icon: 'pi pi-cog',
    route: '/settings',
    active: route.path.startsWith('/settings'),
  },
])

const statusLabel = computed(() => {
  if (statusLoading.value) {
    return 'Checking status'
  }

  if (statusError.value) {
    return 'Status unavailable'
  }

  if (!status.value) {
    return 'Status pending'
  }

  if (syncRunning.value) {
    return 'Circular sync running'
  }

  if ((remoteNewCount.value ?? 0) > 0) {
    return `${remoteNewCount.value?.toLocaleString()} new SBP circular${remoteNewCount.value === 1 ? '' : 's'} found`
  }

  const total = status.value.total_circulars ?? 0
  const departments = status.value.department_count ?? 0
  return `${total.toLocaleString()} circulars / ${departments.toLocaleString()} departments`
})

const statusDetail = computed(() => {
  if (syncRunning.value && status.value?.sync?.started_at) {
    return `Started ${new Date(status.value.sync.started_at).toLocaleString()}`
  }

  if (status.value?.sync?.status === 'failed' && status.value.sync.error) {
    return `Last sync failed: ${status.value.sync.error}`
  }

  if ((remoteNewCount.value ?? 0) > 0) {
    const newest = status.value?.remote_newest || syncStatus.value?.remote_newest
    const label = newest?.reference || newest?.title
    return label ? `Sync recommended. Newest: ${label}` : 'Sync recommended.'
  }

  if (remoteStatus.value === 'checking') {
    return 'Checking SBP for new circulars'
  }

  if (remoteStatus.value === 'error') {
    const error = status.value?.remote_error || syncStatus.value?.remote_error
    return error ? `Could not check SBP: ${error}` : 'Could not check SBP for new circulars'
  }

  if (remoteStatus.value === 'fresh') {
    const checkedAt = status.value?.remote_checked_at || syncStatus.value?.remote_checked_at
    return checkedAt ? `No new SBP circulars found. Checked ${new Date(checkedAt).toLocaleString()}` : 'No new SBP circulars found'
  }

  if (status.value?.last_sync_display) {
    return `Last sync ${status.value.last_sync_display}`
  }

  if (status.value?.vector_db_state) {
    return `Vector DB ${status.value.vector_db_state}`
  }

  return 'API status will appear here when available'
})

const syncStatus = computed<CircularSyncStatus | null>(() => status.value?.sync ?? null)
const syncRunning = computed(() => Boolean(syncStatus.value?.running))
const remoteStatus = computed(() => status.value?.remote_check_status || syncStatus.value?.remote_check_status || null)
const remoteNewCount = computed(() => status.value?.remote_new_count ?? syncStatus.value?.remote_new_count ?? null)
const shouldPollStatus = computed(() => syncRunning.value || remoteStatus.value === 'checking')
const syncStaleness = computed(() => {
  if (statusLoading.value) {
    return 'checking'
  }
  if (statusError.value || syncStatus.value?.status === 'failed') {
    return 'error'
  }
  if (syncRunning.value) {
    return 'running'
  }
  if (remoteStatus.value === 'error') {
    return 'error'
  }
  if (remoteStatus.value === 'checking') {
    return 'checking'
  }
  if ((remoteNewCount.value ?? 0) > 0 || remoteStatus.value === 'new_available') {
    return 'stale'
  }
  if (remoteStatus.value === 'fresh') {
    return 'fresh'
  }
  return 'checking'
})
const syncIcon = computed(() => {
  if (statusLoading.value || syncRunning.value) {
    return 'pi pi-spin pi-refresh'
  }
  if (syncStaleness.value === 'error') {
    return 'pi pi-exclamation-circle'
  }
  if (syncStaleness.value === 'stale') {
    return 'pi pi-exclamation-triangle'
  }
  return 'pi pi-refresh'
})
const syncButtonTitle = computed(() => {
  return `${statusLabel.value}\n${statusDetail.value}`
})

function updateStatusPolling() {
  if (shouldPollStatus.value && !statusPollId) {
    statusPollId = setInterval(() => {
      void loadStatus()
    }, 5000)
    return
  }

  if (!shouldPollStatus.value && statusPollId) {
    clearInterval(statusPollId)
    statusPollId = null
  }
}

async function loadStatus() {
  statusLoading.value = true
  statusError.value = ''

  try {
    status.value = await getAppStatus()
  } catch (error) {
    status.value = null
    statusError.value = error instanceof Error ? error.message : 'Unable to load status'
  } finally {
    statusLoading.value = false
    updateStatusPolling()
  }
}

async function startSync() {
  if (syncRunning.value || syncStarting.value) {
    return
  }

  syncStarting.value = true

  try {
    const sync = await startCircularSync({})
    status.value = {
      ...(status.value || {}),
      sync,
      sync_status: sync.status,
      live_status: sync.live_status,
    }
    updateStatusPolling()
    toast.add({
      severity: 'success',
      summary: 'Circular sync started',
      detail: 'The app remains available while sync runs in the background.',
      life: 3500,
    })
    void loadStatus()
  } catch (error) {
    const apiError = error as ApiError
    toast.add({
      severity: apiError.status === 409 ? 'warn' : 'error',
      summary: apiError.status === 409 ? 'Sync already running' : 'Sync could not start',
      detail: apiError.message,
      life: 4500,
    })
  } finally {
    syncStarting.value = false
  }
}

async function loadLlmStatus() {
  llmLoading.value = true
  llmError.value = ''

  try {
    llmStatus.value = await getLlmStatus()
  } catch (error) {
    llmStatus.value = null
    llmError.value = error instanceof Error ? error.message : 'Unable to check LLM backend'
  } finally {
    llmLoading.value = false
  }
}

const LLM_STATE_LABELS: Record<string, string> = {
  online: 'LLM backend online',
  rate_limited: 'LLM backend rate limited',
  auth_error: 'LLM backend authentication failed',
  not_found: 'LLM model not found',
  offline: 'LLM backend unreachable',
  server_error: 'LLM provider unavailable',
  error: 'LLM backend error',
}

const llmStatusIcon = computed(() => {
  if (llmLoading.value) {
    return 'pi-spin pi-spinner'
  }
  if (llmError.value || !llmStatus.value) {
    return 'pi-question-circle'
  }
  switch (llmStatus.value.state) {
    case 'online':
      return 'pi-bolt'
    case 'rate_limited':
      return 'pi-clock'
    default:
      return 'pi-exclamation-triangle'
  }
})

const llmStatusTone = computed(() => {
  if (llmLoading.value) {
    return 'is-checking'
  }
  if (llmError.value || !llmStatus.value) {
    return 'is-unknown'
  }
  if (llmStatus.value.state === 'online') {
    return 'is-online'
  }
  if (llmStatus.value.state === 'rate_limited') {
    return 'is-warn'
  }
  return 'is-error'
})

const llmStatusLabel = computed(() => {
  if (llmLoading.value) {
    return 'Checking LLM backend'
  }
  if (llmError.value || !llmStatus.value) {
    return 'LLM backend status unavailable'
  }
  return LLM_STATE_LABELS[llmStatus.value.state] ?? 'LLM backend status'
})

const llmStatusDetail = computed(() => {
  if (llmLoading.value || llmError.value || !llmStatus.value) {
    return ''
  }
  const provider = llmStatus.value.provider ? `${llmStatus.value.provider}` : ''
  const model = llmStatus.value.model ? ` · ${llmStatus.value.model}` : ''
  const detail = llmStatus.value.detail ? `\n${llmStatus.value.detail}` : ''
  return `${provider}${model}${detail}`.trim()
})

function syncThemeClass() {
  document.documentElement.classList.toggle('sbpeye-dark', darkMode.value)
  localStorage.setItem('sbpeye-theme', darkMode.value ? 'dark' : 'light')
}

function toggleTheme() {
  darkMode.value = !darkMode.value
  syncThemeClass()
}

onMounted(() => {
  syncThemeClass()
  void loadStatus()
  void loadLlmStatus()
})

onBeforeUnmount(() => {
  if (statusPollId) {
    clearInterval(statusPollId)
  }
})
</script>

<template>
  <div class="app-shell">
    <Toast />
    <ConfirmDialog />

    <nav class="app-sidebar" aria-label="Main navigation">
      <RouterLink to="/circulars" class="sidebar-brand" title="SBPEye — Regulatory intelligence" aria-label="SBPEye home">
        <span class="brand-mark">SBP</span>
      </RouterLink>

      <div class="sidebar-nav">
        <RouterLink
          v-for="item in navItems"
          :key="item.route"
          :to="item.route"
          class="sidebar-nav-item"
          :class="{ 'is-active': item.active }"
          :title="item.label"
          :aria-label="item.label"
          :aria-current="item.active ? 'page' : undefined"
        >
          <span :class="item.icon" />
          <span class="sidebar-nav-label">{{ item.label }}</span>
        </RouterLink>
      </div>

      <div class="sidebar-tools">
        <Button
          text
          rounded
          class="sync-status-button"
          :class="`is-${syncStaleness}`"
          :icon="syncIcon"
          :aria-label="syncRunning ? 'Circular sync running' : 'Sync circulars'"
          :title="syncButtonTitle"
          :disabled="syncRunning || syncStarting"
          @click="startSync"
        />

        <button
          type="button"
          class="sidebar-status llm-status"
          :class="llmStatusTone"
          :title="llmStatusDetail ? `${llmStatusLabel}\n${llmStatusDetail}` : llmStatusLabel"
          :aria-label="llmStatusLabel"
          @click="loadLlmStatus"
        >
          <i class="pi" :class="llmStatusIcon" />
        </button>

        <SbpNewsPanel />

        <a
          class="sidebar-status"
          href="/about.html"
          target="_blank"
          rel="noopener"
          title="About SBPEye"
          aria-label="About SBPEye"
        >
          <i class="pi pi-info-circle" />
        </a>

        <Button
          text
          rounded
          :icon="darkMode ? 'pi pi-sun' : 'pi pi-moon'"
          :aria-label="darkMode ? 'Use light theme' : 'Use dark theme'"
          :title="darkMode ? 'Use light theme' : 'Use dark theme'"
          @click="toggleTheme"
        />
      </div>
    </nav>

    <div class="app-body">
      <Message v-if="statusError" severity="warn" size="small" class="status-message">
        App status endpoint is not available yet.
      </Message>
      <main class="app-main">
        <RouterView />
      </main>
    </div>
  </div>
</template>
