<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { authApi, systemApi } from '@/api'
import { ElMessage } from 'element-plus'

const router = useRouter()
const route = useRoute()
const userStore = useUserStore()

const activeMenu = ref('query')
const HEARTBEAT_INTERVAL_MS = 3 * 60 * 1000

type LlmMonitorStatus = 'checking' | 'up' | 'down'

interface LlmMonitorState {
  status: LlmMonitorStatus
  message: string
}

const llmMonitor = ref<LlmMonitorState>({
  status: 'checking',
  message: '环境启动异常',
})
let llmHeartbeatTimer: number | null = null

const menuItems = [
  { key: 'query', title: '智能取数', icon: 'ChatDotRound', path: '/query' },
  { key: 'config', title: '数据配置', icon: 'Setting', path: '/config' },
  { key: 'history', title: '历史记录', icon: 'Clock', path: '/history' },
  { key: 'settings', title: '系统设置', icon: 'Tools', path: '/settings' },
]

onMounted(async () => {
  try {
    const wsRes = await authApi.getWorkspaces()
    userStore.setWorkspaces(wsRes.data)
  } catch (e) {
    ElMessage.error('获取工作空间失败')
  } finally {
    void checkLlmHeartbeat()
    llmHeartbeatTimer = window.setInterval(() => {
      void checkLlmHeartbeat()
    }, HEARTBEAT_INTERVAL_MS)
  }
})

onBeforeUnmount(() => {
  if (llmHeartbeatTimer !== null) {
    clearInterval(llmHeartbeatTimer)
    llmHeartbeatTimer = null
  }
})

const handleMenuClick = (item: any) => {
  activeMenu.value = item.key
  router.push(item.path)
}

const handleWorkspaceChange = (ws: any) => {
  userStore.setCurrentWorkspace(ws)
}

const handleLogout = () => {
  userStore.logout()
  router.push('/login')
}

const currentPath = computed(() => route.path)
const isFullWidthPage = computed(() => ['/query', '/config'].includes(route.path))
const llmStatusClass = computed(() => {
  if (llmMonitor.value.status === 'up') return 'is-up'
  if (llmMonitor.value.status === 'down') return 'is-down'
  return 'is-checking'
})

const checkLlmHeartbeat = async () => {
  try {
    const res = await systemApi.getLlmHeartbeat()
    const data = res.data
    llmMonitor.value = {
      status: data.ok ? 'up' : 'down',
      message: data.message || (data.ok ? '环境启动正常' : '环境启动异常'),
    }
  } catch (e) {
    llmMonitor.value = {
      status: 'down',
      message: '环境启动异常',
    }
  }
}
</script>

<template>
  <div class="app-container">
    <!-- 左侧边栏（所有页面共用） -->
    <div class="app-sidebar">
      <div class="sidebar-logo">
        <el-icon :size="24"><DataAnalysis /></el-icon>
        <span class="logo-text">智能取数平台</span>
      </div>

      <el-menu
        :default-active="activeMenu"
        class="sidebar-menu"
        :router="false"
      >
        <el-menu-item
          v-for="item in menuItems"
          :key="item.key"
          :index="item.key"
          @click="handleMenuClick(item)"
        >
          <el-icon><component :is="item.icon" /></el-icon>
          <span>{{ item.title }}</span>
        </el-menu-item>
      </el-menu>

      <div class="sidebar-monitor">
        <div class="monitor-main">
          <span class="monitor-dot" :class="llmStatusClass"></span>
          <div class="monitor-message">{{ llmMonitor.message }}</div>
        </div>
      </div>

      <!-- 工作空间选择 -->
      <div class="sidebar-workspace">
        <el-select
          v-model="userStore.currentWorkspace"
          placeholder="选择工作空间"
          @change="handleWorkspaceChange"
          style="width: 100%"
        >
          <el-option
            v-for="ws in userStore.workspaces"
            :key="ws.id"
            :label="ws.name"
            :value="ws"
          />
        </el-select>
      </div>
    </div>

    <!-- 主体区域 -->
    <div class="app-main">
      <!-- 头部 -->
      <div class="main-header">
        <div class="header-title">
          <h3>{{ menuItems.find(m => m.path === currentPath)?.title || '智能取数平台' }}</h3>
        </div>
        <div class="header-actions">
          <el-dropdown @command="handleLogout">
            <span class="user-info">
              <el-icon><User /></el-icon>
              <span>{{ userStore.userInfo?.username || '用户' }}</span>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="logout">退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </div>

      <!-- 内容区域 -->
      <div class="main-content" :class="{ 'no-padding': isFullWidthPage }">
        <router-view />
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.app-container {
  width: 100%;
  height: 100%;
  display: flex;
  background-color: #f5f5f5;
}

// 左侧边栏
.app-sidebar {
  width: 220px;
  background-color: #fff;
  border-right: 1px solid #e4e7ed;
  display: flex;
  flex-direction: column;
  position: fixed;
  top: 0;
  left: 0;
  bottom: 0;
  z-index: 100;
}

.sidebar-logo {
  height: 60px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-bottom: 1px solid #e4e7ed;
  font-size: 18px;
  font-weight: 600;
  color: #409eff;
}

.logo-text {
  margin-left: 8px;
}

.sidebar-menu {
  flex: 1;
  overflow-y: auto;
  border-right: none;
}

.sidebar-monitor {
  margin: 12px 15px 10px;
  padding: 8px 10px;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  background: #fafafa;
}

.monitor-main {
  display: flex;
  align-items: center;
  gap: 8px;
}

.monitor-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex-shrink: 0;

  &.is-up {
    background: #22c55e;
  }

  &.is-down {
    background: #ef4444;
  }

  &.is-checking {
    background: #f59e0b;
  }
}

.monitor-message {
  font-size: 12px;
  color: #303133;
}

.sidebar-workspace {
  padding: 15px;
  border-top: 1px solid #e4e7ed;
}

// 主体区域
.app-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  margin-left: 220px;
  transition: margin-left 0.3s ease;

  &.full-width {
    margin-left: 0;
  }
}

.main-header {
  height: 60px;
  background-color: #fff;
  border-bottom: 1px solid #e4e7ed;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
  flex-shrink: 0;
}

.header-title h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 500;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 16px;
}

.user-info {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
}

.main-content {
  flex: 1;
  padding: 20px;
  overflow-y: auto;

  &.no-padding {
    padding: 0;
  }
}
</style>
