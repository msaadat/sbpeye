<script setup lang="ts">
/**
 * The admin console shell: the gate, the tab bar, and nothing else.
 *
 * Every tab is a child route (see `router/index.ts`), so this file holds only what all
 * of them share. The admin check lives here rather than in each tab for the same reason
 * the tab bar does — one place to be wrong, and a non-admin reads one explanation
 * instead of five.
 */
import { computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import Message from 'primevue/message'

import { useCurrentUser } from '@/lib/useCurrentUser'

const route = useRoute()
const { user: currentUser, isAdmin, load: loadCurrentUser } = useCurrentUser()

const tabs = [
  { label: 'Corpus', icon: 'pi pi-database', to: '/admin/corpus' },
  { label: 'Index', icon: 'pi pi-sitemap', to: '/admin/index' },
  { label: 'Sync', icon: 'pi pi-cloud-download', to: '/admin/sync' },
  { label: 'Runs', icon: 'pi pi-history', to: '/admin/runs' },
  { label: 'Users', icon: 'pi pi-users', to: '/admin/users' },
  { label: 'Deployment', icon: 'pi pi-server', to: '/admin/deployment' },
]

const activeTab = computed(() => tabs.find((tab) => route.path.startsWith(tab.to)))

onMounted(loadCurrentUser)
</script>

<template>
  <div class="admin-view">
    <header class="admin-header">
      <h1>Admin console</h1>
      <p>Corpus and index status, corpus sync, run history, accounts, and deployment configuration.</p>
    </header>

    <!--
      The server is the authority here: every route these tabs call is admin-gated and
      returns 403 regardless. This is so a non-admin who types the URL reads an
      explanation rather than a page of failed requests.
    -->
    <Message v-if="!isAdmin" severity="warn" :closable="false">
      This page is for administrators. Signed in as {{ currentUser?.email }}.
    </Message>

    <template v-else>
      <nav class="admin-tabs" aria-label="Admin sections">
        <RouterLink
          v-for="tab in tabs"
          :key="tab.to"
          :to="tab.to"
          class="admin-tab"
          :class="{ 'is-active': activeTab?.to === tab.to }"
          :aria-current="activeTab?.to === tab.to ? 'page' : undefined"
        >
          <i :class="tab.icon" aria-hidden="true" />
          <span>{{ tab.label }}</span>
        </RouterLink>
      </nav>

      <RouterView />
    </template>
  </div>
</template>

<style scoped>
.admin-view {
  display: flex;
  flex-direction: column;
  gap: 1.25rem;
  padding: 1.5rem;
  max-width: 72rem;
}

.admin-header h1 {
  margin: 0;
  font-size: 1.35rem;
}

.admin-header p {
  margin: 0.25rem 0 0;
  color: var(--text-muted, #6b7280);
  font-size: 0.875rem;
}

.admin-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem;
  border-bottom: 1px solid var(--surface-border, #d1d5db);
  margin-bottom: -0.25rem;
}

.admin-tab {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.6rem 0.9rem;
  font-size: 0.875rem;
  color: var(--text-muted, #6b7280);
  text-decoration: none;
  border-bottom: 2px solid transparent;
  /* Reserve the bold weight's width up front so the row does not reflow on selection. */
  transition: color 0.15s ease, border-color 0.15s ease;
}

.admin-tab:hover {
  color: var(--p-text-color, inherit);
}

.admin-tab.is-active {
  color: var(--p-primary-color, #2563eb);
  border-bottom-color: var(--p-primary-color, #2563eb);
  font-weight: 600;
}

.admin-tab i {
  font-size: 0.8125rem;
}
</style>
