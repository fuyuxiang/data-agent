import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/ask' },
    { path: '/ask', name: 'ask', component: () => import('@/views/WorkbenchView.vue') },
    {
      path: '/trace/:turnId',
      name: 'trace',
      component: () => import('@/views/TraceView.vue'),
    },
    {
      path: '/admin/datasets',
      name: 'datasets',
      component: () => import('@/views/DatasetsView.vue'),
    },
    {
      path: '/admin/datasets/:name',
      name: 'dataset-detail',
      component: () => import('@/views/DatasetDetailView.vue'),
    },
  ],
})

export default router