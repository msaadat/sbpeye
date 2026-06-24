<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import Button from 'primevue/button'
import ConfirmDialog from 'primevue/confirmdialog'
import Message from 'primevue/message'
import Toast from 'primevue/toast'
import SbpNewsPanel from '@/components/SbpNewsPanel.vue'
import { getAppStatus, type AppStatus } from '@/lib/api'

const route = useRoute()
const darkMode = ref(localStorage.getItem('sbpeye-theme') === 'dark')
const status = ref<AppStatus | null>(null)
const statusLoading = ref(false)
const statusError = ref('')

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

  const total = status.value.total_circulars ?? 0
  const departments = status.value.department_count ?? 0
  return `${total.toLocaleString()} circulars / ${departments.toLocaleString()} departments`
})

const statusDetail = computed(() => {
  if (status.value?.last_sync_display) {
    return `Last sync ${status.value.last_sync_display}`
  }

  if (status.value?.vector_db_state) {
    return `Vector DB ${status.value.vector_db_state}`
  }

  return 'API status will appear here when available'
})

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
  }
}

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
        <div
          class="sidebar-status"
          :title="`${statusLabel}\n${statusDetail}`"
          :aria-label="statusLabel"
        >
          <i
            class="pi"
            :class="{
              'pi-spin pi-spinner': statusLoading,
              'pi-exclamation-circle': statusError,
              'pi-check-circle': !statusLoading && !statusError,
            }"
          />
        </div>

        <SbpNewsPanel />

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
