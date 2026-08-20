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
      path: '/admin',
      name: 'admin',
      component: () => import('@/views/AdminView.vue'),
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
