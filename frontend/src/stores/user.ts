import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { User, Workspace } from '@/api/types'

export const useUserStore = defineStore('user', () => {
  const token = ref<string | null>(localStorage.getItem('token'))
  const userInfo = ref<User | null>(null)
  const workspaces = ref<Workspace[]>([])
  const currentWorkspace = ref<Workspace | null>(null)

  const isLoggedIn = computed(() => !!token.value)

  function setToken(newToken: string) {
    token.value = newToken
    localStorage.setItem('token', newToken)
  }

  function setUserInfo(info: User) {
    userInfo.value = info
  }

  function setWorkspaces(list: Workspace[]) {
    workspaces.value = list
    if (list.length > 0 && !currentWorkspace.value) {
      currentWorkspace.value = list[0]
    }
  }

  function setCurrentWorkspace(ws: Workspace) {
    currentWorkspace.value = ws
    localStorage.setItem('currentWorkspace', String(ws.id))
  }

  function logout() {
    token.value = null
    userInfo.value = null
    localStorage.removeItem('token')
    localStorage.removeItem('currentWorkspace')
  }

  return {
    token,
    userInfo,
    workspaces,
    currentWorkspace,
    isLoggedIn,
    setToken,
    setUserInfo,
    setWorkspaces,
    setCurrentWorkspace,
    logout,
  }
})
