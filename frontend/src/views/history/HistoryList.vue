<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useUserStore } from '@/stores/user'
import { historyApi, datasetApi } from '@/api'
import type { QueryHistory, Dataset } from '@/api/types'
import { ElMessage } from 'element-plus'

const userStore = useUserStore()

const loading = ref(false)
const histories = ref<QueryHistory[]>([])
const datasets = ref<Dataset[]>([])
const selectedDataset = ref<number | undefined>()

const loadData = async () => {
  if (!userStore.currentWorkspace) return

  loading.value = true
  try {
    const [hRes, dsRes] = await Promise.all([
      historyApi.list(userStore.currentWorkspace.id, selectedDataset.value),
      datasetApi.list(userStore.currentWorkspace.id),
    ])
    histories.value = hRes.data
    datasets.value = dsRes.data
  } catch (e) {
    ElMessage.error('加载失败')
  } finally {
    loading.value = false
  }
}

onMounted(loadData)

const statusLabels: Record<string, string> = {
  success: '成功',
  error: '失败',
}

const getDatasetName = (id?: number) => {
  if (!id) return '-'
  return datasets.value.find((d) => d.id === id)?.name || '-'
}
</script>

<template>
  <div class="history-list">
    <div class="toolbar">
      <el-select v-model="selectedDataset" placeholder="筛选数据集" clearable @change="loadData">
        <el-option v-for="ds in datasets" :key="ds.id" :label="ds.name" :value="ds.id" />
      </el-select>
      <el-button @click="loadData"><el-icon><Refresh /></el-icon>刷新</el-button>
    </div>

    <el-table v-loading="loading" :data="histories" border stripe>
      <el-table-column prop="question" label="问题" min-width="200" show-overflow-tooltip />
      <el-table-column label="数据集" width="120">
        <template #default="{ row }">{{ getDatasetName(row.dataset_id) }}</template>
      </el-table-column>
      <el-table-column prop="intent" label="意图" width="80" />
      <el-table-column label="结果" width="80">
        <template #default="{ row }">
          <el-tag :type="row.status === 'success' ? 'success' : 'danger'" size="small">
            {{ statusLabels[row.status] }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="row_count" label="行数" width="80" />
      <el-table-column prop="execution_time_ms" label="耗时" width="80">
        <template #default="{ row }">
          {{ row.execution_time_ms ? (row.execution_time_ms / 1000).toFixed(2) + 's' : '-' }}
        </template>
      </el-table-column>
      <el-table-column prop="trace_id" label="Trace ID" width="150" />
      <el-table-column prop="created_at" label="时间" width="180">
        <template #default="{ row }">{{ new Date(row.created_at).toLocaleString() }}</template>
      </el-table-column>
    </el-table>

    <div v-if="!loading && histories.length === 0" class="empty-state">
      <el-icon class="empty-state-icon"><Clock /></el-icon>
      <p>暂无查询历史</p>
    </div>
  </div>
</template>

<style scoped lang="scss">
.history-list { padding: 20px; }
.toolbar { margin-bottom: 20px; display: flex; gap: 10px; }
.empty-state { text-align: center; padding: 60px; color: #909399; .empty-state-icon { font-size: 64px; margin-bottom: 20px; } }
</style>
