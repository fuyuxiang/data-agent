import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'
import { useUserStore } from '@/stores/user'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('@/views/auth/Login.vue'),
    meta: { requiresAuth: false },
  },
  {
    path: '/',
    component: () => import('@/layouts/MainLayout.vue'),
    redirect: '/query',
    meta: { requiresAuth: true },
    children: [
      {
        path: 'query',
        name: 'Query',
        component: () => import('@/views/query/QueryPage.vue'),
      },
      {
        path: 'config',
        name: 'DataConfig',
        component: () => import('@/views/config/DataConfig.vue'),
      },
      {
        path: 'history',
        name: 'History',
        component: () => import('@/views/history/HistoryList.vue'),
      },
      {
        path: 'settings',
        name: 'Settings',
        component: () => import('@/views/settings/SettingsPage.vue'),
      },
    ],
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach((to, _from, next) => {
  const userStore = useUserStore()

  if (to.meta.requiresAuth !== false && !userStore.isLoggedIn) {
    next('/login')
  } else if (to.path === '/login' && userStore.isLoggedIn) {
    next('/query')
  } else {
    next()
  }
})

export default router
