<script setup lang="ts">
/**
 * The admin console shell: the gate, the section bar, and nothing else.
 *
 * Every tab is a child route (see `router/index.ts`), so this file holds only what all
 * of them share. The admin check lives here rather than in each tab for the same reason
 * the tab bar does — one place to be wrong, and a non-admin reads one explanation
 * instead of five.
 *
 * One row, five sections, detail on sub-tabs inside the section. The console used to
 * carry two stacked strips of twelve links, three of which duplicated a concept in the
 * other row, and the second strip never lit up because only the first was matched
 * against the route. Nesting is what removes both problems: the sub-tabs of the open
 * section are the only ones on screen, so there is nothing left to duplicate, and
 * `matches()` below is used for sections and children alike.
 */
import { computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import Message from 'primevue/message'

import { useCurrentUser } from '@/lib/useCurrentUser'

interface AdminLink {
  label: string
  to: string
}

interface AdminSection extends AdminLink {
  icon: string
  blurb: string
  children?: AdminLink[]
}

const route = useRoute()
const { user: currentUser, isAdmin, load: loadCurrentUser } = useCurrentUser()

const sections: AdminSection[] = [
  {
    label: 'Overview',
    icon: 'pi pi-home',
    to: '/admin/overview',
    blurb: 'Where the corpus stands, stage by stage, and what is waiting on you.',
  },
  {
    label: 'Ingest',
    icon: 'pi pi-cloud-download',
    to: '/admin/ingest',
    blurb: 'Getting circulars out of SBP and into the database.',
    children: [
      { label: 'Listing audit', to: '/admin/ingest/audit' },
      { label: 'Gap queue', to: '/admin/ingest/gaps' },
      { label: 'Backfill', to: '/admin/ingest/backfill' },
      { label: 'Source connectivity', to: '/admin/ingest/source' },
    ],
  },
  {
    label: 'Documents',
    icon: 'pi pi-file',
    to: '/admin/documents',
    blurb: 'What the corpus holds, and completing what it is missing.',
    children: [
      { label: 'Workbench', to: '/admin/documents/workbench' },
      { label: 'Corpus statistics', to: '/admin/documents/corpus' },
      { label: 'Library', to: '/admin/documents/library' },
    ],
  },
  {
    label: 'Jobs',
    icon: 'pi pi-history',
    to: '/admin/jobs',
    blurb: 'Everything this server has run against the corpus, newest first.',
  },
  {
    label: 'System',
    icon: 'pi pi-server',
    to: '/admin/system',
    blurb: 'The search index, the deployment it runs on, and who may sign in.',
    children: [
      { label: 'Search index', to: '/admin/system/search-index' },
      { label: 'Storage & providers', to: '/admin/system/storage' },
      { label: 'Users', to: '/admin/system/users' },
    ],
  },
]

/**
 * Whether `path` is inside `base`.
 *
 * Prefix rather than equality so a section stays lit while one of its sub-tabs is open,
 * and the `/` guard so `/admin/jobs` is not read as being inside `/admin/job`.
 */
function matches(path: string, base: string): boolean {
  return path === base || path.startsWith(`${base}/`)
}

const activeSection = computed(() => sections.find((section) => matches(route.path, section.to)))
const activeChild = computed(() =>
  activeSection.value?.children?.find((child) => matches(route.path, child.to)),
)

onMounted(loadCurrentUser)
</script>

<template>
  <div class="admin-view">
    <header class="admin-header">
      <h1>Admin console</h1>
      <p>{{ activeSection?.blurb ?? 'Audit the corpus, complete missing work, and keep the deployment honest.' }}</p>
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
          v-for="section in sections"
          :key="section.to"
          :to="section.to"
          class="admin-tab"
          :class="{ 'is-active': activeSection?.to === section.to }"
          :aria-current="activeSection?.to === section.to ? 'page' : undefined"
        >
          <i :class="section.icon" aria-hidden="true" />
          <span>{{ section.label }}</span>
        </RouterLink>
      </nav>

      <nav
        v-if="activeSection?.children"
        class="admin-subtabs"
        :aria-label="`${activeSection.label} sections`"
      >
        <RouterLink
          v-for="child in activeSection.children"
          :key="child.to"
          :to="child.to"
          class="admin-subtab"
          :class="{ 'is-active': activeChild?.to === child.to }"
          :aria-current="activeChild?.to === child.to ? 'page' : undefined"
        >
          {{ child.label }}
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
  /* Wide on purpose: the tabs beneath render eight-column tables, and the console used
     to cap at 72rem — the widest content in the app under the tightest measure. */
  max-width: 96rem;
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

/* Sub-tabs read as belonging to the section above them: pills rather than a second
   underlined strip, which is what made the old bottom row look like a rival nav. */
.admin-subtabs {
  display: flex;
  flex-wrap: wrap;
  gap: 0.3rem;
}

.admin-subtab {
  padding: 0.3rem 0.7rem;
  font-size: 0.8125rem;
  color: var(--sbp-muted, #6b7280);
  text-decoration: none;
  border: 1px solid var(--sbp-border, #d1d5db);
  border-radius: var(--sbp-radius-pill, 999px);
  transition: color 0.15s ease, border-color 0.15s ease, background 0.15s ease;
}

.admin-subtab:hover {
  color: var(--p-text-color, inherit);
  border-color: var(--sbp-green, #156f52);
}

.admin-subtab.is-active {
  color: var(--sbp-green, #156f52);
  border-color: var(--sbp-green, #156f52);
  background: var(--sbp-subtle, #eef2ed);
  font-weight: 600;
}
</style>
