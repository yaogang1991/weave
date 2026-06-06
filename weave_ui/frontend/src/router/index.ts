import { createRouter, createWebHistory } from 'vue-router'
import MainLayout from '../layouts/MainLayout.vue'
const routes = [
  { path: '/', component: MainLayout, children: [
    { path: '', name: 'dashboard', component: () => import('../views/DashboardView.vue') },
    { path: 'tasks', name: 'tasks', component: () => import('../views/TasksView.vue') },
    { path: 'history', name: 'history', component: () => import('../views/HistoryView.vue') },
    { path: 'settings', name: 'settings', component: () => import('../views/SettingsView.vue') },
  ]},
]
const router = createRouter({ history: createWebHistory(), routes })
export default router
