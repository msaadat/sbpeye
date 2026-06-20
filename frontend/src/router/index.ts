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
      path: '/chat',
      name: 'chat',
      component: () => import('@/views/ChatView.vue'),
    },
    {
      path: '/documents/open',
      name: 'document-open',
      component: () => import('@/views/DocumentView.vue'),
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
      path: '/:pathMatch(.*)*',
      redirect: '/circulars',
    },
  ],
})

export default router
