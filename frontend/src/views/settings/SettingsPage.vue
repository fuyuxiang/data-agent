<script setup lang="ts">
import { ref } from 'vue'
import { useUserStore } from '@/stores/user'
import { authApi } from '@/api'
import { ElMessage } from 'element-plus'

const userStore = useUserStore()
const wsForm = ref({ name: '', description: '' })
const creating = ref(false)

const handleCreateWs = async () => {
  if (!wsForm.value.name) {
    ElMessage.warning('请输入工作空间名称')
    return
  }

  creating.value = true
  try {
    const res = await authApi.createWorkspace(wsForm.value)
    userStore.setWorkspaces([...userStore.workspaces, res.data])
    ElMessage.success('创建成功')
    wsForm.value = { name: '', description: '' }
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || '创建失败')
  } finally {
    creating.value = false
  }
}
</script>

<template>
  <div class="settings-page">
    <el-row :gutter="20">
      <el-col :span="12">
        <el-card>
          <template #header>
            <h3>用户信息</h3>
          </template>
          <el-descriptions :column="1" border>
            <el-descriptions-item label="用户名">{{ userStore.userInfo?.username }}</el-descriptions-item>
            <el-descriptions-item label="邮箱">{{ userStore.userInfo?.email || '-' }}</el-descriptions-item>
            <el-descriptions-item label="姓名">{{ userStore.userInfo?.full_name || '-' }}</el-descriptions-item>
          </el-descriptions>
        </el-card>
      </el-col>

      <el-col :span="12">
        <el-card>
          <template #header>
            <h3>创建工作空间</h3>
          </template>
          <el-form label-width="100px">
            <el-form-item label="名称">
              <el-input v-model="wsForm.name" placeholder="工作空间名称" />
            </el-form-item>
            <el-form-item label="描述">
              <el-input v-model="wsForm.description" type="textarea" :rows="2" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="creating" @click="handleCreateWs">创建</el-button>
            </el-form-item>
          </el-form>
        </el-card>
      </el-col>
    </el-row>

    <el-card style="margin-top: 20px;">
      <template #header>
        <h3>关于</h3>
      </template>
      <p>智能问数平台 v1.0.0</p>
      <p style="color: #909399; font-size: 12px;">企业级自然语言数据查询与分析平台</p>
    </el-card>
  </div>
</template>

<style scoped lang="scss">
.settings-page { padding: 20px; }
h3 { margin: 0; font-size: 16px; font-weight: 500; }
</style>
