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
        { path: '', redirect: '/admin/corpus' },
        {
          path: 'corpus',
          name: 'admin-corpus',
          component: () => import('@/views/admin/AdminCorpusTab.vue'),
        },
        {
          path: 'index',
          name: 'admin-index',
          component: () => import('@/views/admin/AdminIndexTab.vue'),
        },
        {
          path: 'runs',
          name: 'admin-runs',
          component: () => import('@/views/admin/AdminRunsTab.vue'),
        },
        {
          path: 'users',
          name: 'admin-users',
          component: () => import('@/views/admin/AdminUsersTab.vue'),
        },
        {
          path: 'deployment',
          name: 'admin-deployment',
          component: () => import('@/views/admin/AdminDeploymentTab.vue'),
        },
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
