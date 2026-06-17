<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import Button from 'primevue/button'
import ConfirmDialog from 'primevue/confirmdialog'
import Menubar from 'primevue/menubar'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import Toast from 'primevue/toast'
import ToggleSwitch from 'primevue/toggleswitch'
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

    <header class="app-header">
      <Menubar :model="navItems" class="app-nav">
        <template #start>
          <RouterLink to="/circulars" class="brand">
            <span class="brand-mark">SBP</span>
            <span>
              <strong>SBPEye</strong>
              <small>Regulatory intelligence</small>
            </span>
          </RouterLink>
        </template>

        <template #item="{ item, props }">
          <RouterLink
            v-if="item.route"
            v-slot="{ href, navigate }"
            :to="item.route"
            custom
          >
            <a
              v-bind="props.action"
              :href="href"
              :class="{ 'is-active': item.active }"
              @click="navigate"
            >
              <span :class="item.icon" />
              <span>{{ item.label }}</span>
            </a>
          </RouterLink>
        </template>

        <template #end>
          <div class="header-tools">
            <div class="status-chip" :title="statusDetail">
              <i
                class="pi"
                :class="{
                  'pi-spin pi-spinner': statusLoading,
                  'pi-exclamation-circle': statusError,
                  'pi-check-circle': !statusLoading && !statusError,
                }"
              />
              <span>{{ statusLabel }}</span>
            </div>

            <SbpNewsPanel />

            <Button
              text
              rounded
              :icon="darkMode ? 'pi pi-moon' : 'pi pi-sun'"
              aria-label="Toggle theme"
              @click="toggleTheme"
            />
            <ToggleSwitch
              v-model="darkMode"
              aria-label="Dark theme"
              @change="syncThemeClass"
            />
          </div>
        </template>
      </Menubar>

      <Message v-if="statusError" severity="warn" size="small" class="status-message">
        App status endpoint is not available yet.
      </Message>
    </header>

    <main class="app-main">
      <RouterView />
    </main>

    <footer class="app-footer">
      <span>Analyst workspace</span>
      <Tag severity="success" value="Vue + PrimeVue" />
    </footer>
  </div>
</template>
