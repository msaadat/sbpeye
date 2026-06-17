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
      path: '/circulars/:id',
      name: 'circular-detail',
      component: () => import('@/views/CircularDetailView.vue'),
      props: true,
    },
    {
      path: '/chat',
      name: 'chat',
      component: () => import('@/views/ChatView.vue'),
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
