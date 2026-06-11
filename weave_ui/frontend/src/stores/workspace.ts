import { ref } from 'vue'
import { defineStore } from 'pinia'
import type { Workspace } from '../types'
import * as api from '../api'

export const useWorkspaceStore = defineStore('workspace', () => {
  const workspaces = ref<Workspace[]>([])
  const loading = ref(false)

  async function fetchWorkspaces() {
    loading.value = true
    try {
      const res = await api.getWorkspaces()
      workspaces.value = res.workspaces
    } catch { workspaces.value = [] }
    finally { loading.value = false }
  }

  async function addWorkspace(path: string, label: string) {
    const ws = await api.addWorkspace(path, label)
    await fetchWorkspaces()
    return ws
  }

  return { workspaces, loading, fetchWorkspaces, addWorkspace }
})
