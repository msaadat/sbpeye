<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Button from 'primevue/button'
import ConfirmDialog from 'primevue/confirmdialog'
import Message from 'primevue/message'
import Toast from 'primevue/toast'
import { useToast } from 'primevue/usetoast'
import SbpNewsPanel from '@/components/SbpNewsPanel.vue'
import { getAppStatus, logout, startCircularSync, type ApiError, type AppStatus, type CircularSyncStatus } from '@/lib/api'
import { adminOnlyHint, adminOnlyLabel } from '@/lib/adminOnly'
import { useCurrentUser } from '@/lib/useCurrentUser'
import { useLlmDebugState } from '@/lib/useLlmDebugState'
import { useLlmStatus } from '@/lib/useLlmStatus'

const route = useRoute()
const router = useRouter()
const toast = useToast()
const { state: llmDebugState, refreshLlmDebugState } = useLlmDebugState()
const { email: userEmail, isAdmin, load: loadCurrentUser, clear: clearCurrentUser } = useCurrentUser()
const darkMode = ref(localStorage.getItem('sbpeye-theme') === 'dark')
const status = ref<AppStatus | null>(null)
const statusLoading = ref(false)
const statusError = ref('')
// Shared with the chat view, which shows its own reminder when this account has no
// provider key. One probe, one answer, no chance of the two disagreeing.
const {
  status: llmStatus,
  loading: llmLoading,
  error: llmError,
  load: loadLlmStatus,
} = useLlmStatus()
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
    label: 'Laws',
    icon: 'pi pi-book',
    route: '/laws',
    active: route.path.startsWith('/laws'),
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
  ...(isAdmin.value ? [{
    label: 'Admin',
    icon: 'pi pi-users',
    route: '/admin',
    active: route.path.startsWith('/admin'),
  }] : []),
  ...(llmDebugState.value.effective && isAdmin.value ? [{
    label: 'Debug',
    icon: 'pi pi-desktop',
    route: '/debug',
    active: route.path.startsWith('/debug'),
  }] : []),
])

async function signOut(): Promise<void> {
  try {
    await logout()
  } catch {
    // Even if the call fails the cookie may already be gone; the redirect below is what
    // actually matters, and the login page is the right place to end up either way.
  }
  clearCurrentUser()
  window.location.assign('/login')
}

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
  const base = `${statusLabel.value}\n${statusDetail.value}`
  // The badge keeps reporting freshness to everyone — that is worth knowing whoever you
  // are. Only starting a sync belongs to the admin, so only that is spelled out here.
  return isAdmin.value ? base : `${base}\n${adminOnlyHint('Starting a sync')}`
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

const LLM_STATE_LABELS: Record<string, string> = {
  online: 'LLM backend online',
  not_configured: 'No AI provider configured',
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
    case 'not_configured':
      return 'pi-key'
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
  // Nothing is broken when a provider is simply unset, so it is not painted as a
  // failure; the badge points at Settings instead.
  if (llmStatus.value.state === 'rate_limited' || llmStatus.value.state === 'not_configured') {
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

function onLlmStatusClick() {
  // Re-probing an unset provider can only return the same answer, so the badge offers
  // the action that would change it instead.
  if (llmStatus.value?.state === 'not_configured') {
    void router.push('/settings')
    return
  }
  void loadLlmStatus(true)
}

function syncThemeClass() {
  document.documentElement.classList.toggle('sbpeye-dark', darkMode.value)
  localStorage.setItem('sbpeye-theme', darkMode.value ? 'dark' : 'light')
}

function toggleTheme() {
  darkMode.value = !darkMode.value
  syncThemeClass()
}

onMounted(async () => {
  syncThemeClass()
  void loadStatus()
  void loadLlmStatus()
  // Awaited: the nav needs to know whether to show Admin, and the debug probe below is
  // admin-only. Firing it for a tester is a guaranteed 403 and an unhandled rejection in
  // their console for a panel they cannot open anyway.
  await loadCurrentUser()
  if (isAdmin.value) {
    void refreshLlmDebugState()
  }
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
        <!-- The title sits on the wrapper: a disabled button swallows its own hover
             events, and the explanation is needed exactly when it is disabled. -->
        <span class="admin-gate" :title="syncButtonTitle">
          <Button
            text
            rounded
            class="sync-status-button"
            :class="`is-${syncStaleness}`"
            :icon="syncIcon"
            :aria-label="isAdmin
              ? (syncRunning ? 'Circular sync running' : 'Sync circulars')
              : adminOnlyLabel('Circular sync status')"
            :disabled="!isAdmin || syncRunning || syncStarting"
            @click="startSync"
          />
        </span>

        <button
          type="button"
          class="sidebar-status llm-status"
          :class="llmStatusTone"
          :title="llmStatusDetail ? `${llmStatusLabel}\n${llmStatusDetail}` : llmStatusLabel"
          :aria-label="llmStatusLabel"
          @click="onLlmStatusClick"
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

        <Button
          v-if="userEmail"
          text
          rounded
          icon="pi pi-sign-out"
          class="sign-out-button"
          :aria-label="`Sign out of ${userEmail}`"
          :title="`Signed in as ${userEmail} — sign out`"
          @click="signOut"
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
