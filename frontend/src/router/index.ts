import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory('/'),
  routes: [
    {
      path: '/',
      redirect: '/circulars',
    },
    {
      path: '/circulars',
      name: 'circulars',
      component: () => import('@/views/CircularsView.vue'),
    },
    {
      path: '/circulars/open',
      name: 'circular-open',
      component: () => import('@/views/CircularUrlView.vue'),
    },
    {
      path: '/circulars/:id',
      name: 'circular-detail',
      component: () => import('@/views/CircularsView.vue'),
    },
    {
      path: '/laws',
      name: 'laws',
      component: () => import('@/views/LawsView.vue'),
    },
    {
      path: '/laws/:id',
      name: 'law-detail',
      component: () => import('@/views/LawsView.vue'),
    },
    {
      path: '/chat',
      name: 'chat',
      component: () => import('@/views/ChatView.vue'),
    },
    {
      // Conversations are addressable so a thread can be bookmarked, shared and
      // survive a reload. Workspace sessions carry a "workspace:" prefix, which
      // the router encodes into the single param.
      path: '/chat/:sessionId',
      name: 'chat-session',
      component: () => import('@/views/ChatView.vue'),
    },
    {
      path: '/documents/open',
      name: 'document-open',
      component: () => import('@/views/DocumentView.vue'),
    },
    {
      path: '/values',
      name: 'values',
      component: () => import('@/views/RegulatoryValuesView.vue'),
    },
    {
      path: '/ecodata',
      name: 'ecodata',
      component: () => import('@/views/EcoDataView.vue'),
    },
    {
      path: '/settings',
      name: 'settings',
      component: () => import('@/views/SettingsView.vue'),
    },
    {
      // Admin-only server-side too; the view explains itself rather than 403-ing blankly.
      //
      // Tabs are child routes rather than local state so each one is addressable: a
      // link to the index-health tab is a thing an operator wants to send someone, and
      // a reload has to land back where they were rather than on the default.
      path: '/admin',
      component: () => import('@/views/AdminView.vue'),
      children: [
        { path: '', redirect: '/admin/overview' },
        { path: 'overview', name: 'admin-overview', component: () => import('@/views/admin/AdminOverviewTab.vue') },

        // Ingest — everything between SBP's listing and a row in our database.
        { path: 'ingest', redirect: '/admin/ingest/audit' },
        // Three views over the same audit/queue/backfill state, so one component holds
        // it and reads the last path segment to decide which third to show. Splitting it
        // three ways would mean three copies of the 2-second poll and the job it follows.
        ...['audit', 'gaps', 'backfill'].map(view => ({
          path: `ingest/${view}`, component: () => import('@/views/admin/AdminMirrorTab.vue'),
        })),
        { path: 'ingest/source', name: 'admin-ingest-source', component: () => import('@/views/admin/AdminSyncTab.vue') },

        // Documents — what we hold, measured and repaired.
        { path: 'documents', redirect: to => ({ path: '/admin/documents/workbench', query: to.query }) },
        {
          path: 'documents/workbench',
          name: 'admin-workbench',
          component: () => import('@/views/admin/AdminWorkbenchTab.vue'),
        },
        {
          path: 'documents/corpus',
          name: 'admin-corpus',
          component: () => import('@/views/admin/AdminCorpusTab.vue'),
        },
        // Admin-uploaded laws (docs/LAWS_UPLOADS_PLAN.md). The plan named this
        // `/admin/library` when the console was a flat row of tabs; under Documents is
        // where it belongs now that the console is sectioned, beside the other two views
        // of what the corpus holds.
        {
          path: 'documents/library',
          name: 'admin-library',
          component: () => import('@/views/admin/AdminLibraryTab.vue'),
        },

        { path: 'jobs', name: 'admin-jobs', component: () => import('@/views/admin/AdminJobsTab.vue') },

        // System — the machine rather than the corpus.
        { path: 'system', redirect: '/admin/system/search-index' },
        {
          path: 'system/search-index',
          name: 'admin-index',
          component: () => import('@/views/admin/AdminIndexTab.vue'),
        },
        {
          path: 'system/storage',
          name: 'admin-deployment',
          component: () => import('@/views/admin/AdminDeploymentTab.vue'),
        },
        {
          path: 'system/users',
          name: 'admin-users',
          component: () => import('@/views/admin/AdminUsersTab.vue'),
        },

        //
        // The pre-consolidation paths. Operators send each other links to these — a
        // batch's follow-up link carried `?source_job_id=`, and the mirror tab was
        // bookmarked — so they redirect rather than 404. The three that became one
        // workbench carry their old identity through as `scope`.
        //
        { path: 'mirror', redirect: '/admin/ingest/audit' },
        { path: 'sync', redirect: '/admin/ingest/source' },
        {
          path: 'analysis',
          redirect: to => ({ path: '/admin/documents/workbench', query: { ...to.query, scope: 'ai' } }),
        },
        {
          path: 'search-index',
          redirect: to => ({ path: '/admin/documents/workbench', query: { ...to.query, scope: 'index' } }),
        },
        { path: 'corpus', redirect: '/admin/documents/corpus' },
        { path: 'index', redirect: '/admin/system/search-index' },
        { path: 'runs', redirect: '/admin/jobs' },
        { path: 'users', redirect: '/admin/system/users' },
        { path: 'deployment', redirect: '/admin/system/storage' },
      ],
    },
    {
      path: '/debug',
      name: 'debug',
      component: () => import('@/views/DebugView.vue'),
    },
    {
      path: '/:pathMatch(.*)*',
      redirect: '/circulars',
    },
  ],
})

export default router
