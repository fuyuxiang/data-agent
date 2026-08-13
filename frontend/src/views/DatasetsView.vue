<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { ApiError } from '@/api/client'
import { listDatasets } from '@/api/semantic'
import type { DatasetSummary } from '@/api/types'

const datasets = ref<DatasetSummary[]>([])
const error = ref('')
const loaded = ref(false)

onMounted(async () => {
  try {
    datasets.value = await listDatasets()
  } catch (raised) {
    error.value = raised instanceof ApiError ? raised.message : '加载失败'
  } finally {
    loaded.value = true
  }
})
</script>

<template>
  <div class="datasets">
    <header class="datasets__header">
      <router-link to="/ask">← 返回工作台</router-link>
      <h2>数据集</h2>
    </header>

    <div v-if="error" class="datasets__error" data-test="datasets-error">{{ error }}</div>

    <table v-else-if="datasets.length" class="datasets__table" data-test="datasets-table">
      <thead>
        <tr>
          <th>名称</th>
          <th>状态</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in datasets" :key="row.name" data-test="dataset-row">
          <td>
            <router-link
              data-test="dataset-link"
              :to="{ name: 'dataset-detail', params: { name: row.name } }"
            >
              {{ row.business_name }}（{{ row.name }}）
            </router-link>
          </td>
          <td>
            <span :class="['datasets__status', row.is_published ? 'datasets__status--ok' : 'datasets__status--no']">
              {{ row.is_published ? '已发布' : '未发布' }}
            </span>
          </td>
        </tr>
      </tbody>
    </table>

    <div v-else-if="loaded" class="datasets__empty">还没有数据集</div>
  </div>
</template>

<style scoped>
.datasets {
  padding: 16px;
}

.datasets__header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}

.datasets__header h2 {
  margin: 0;
  font-size: 16px;
}

.datasets__error {
  padding: 12px;
  background: var(--el-color-error-light-9);
  color: var(--el-color-error);
  border-radius: 4px;
}

.datasets__empty {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.datasets__table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.datasets__table th,
.datasets__table td {
  padding: 6px 8px;
  border: 1px solid var(--el-border-color-light);
  text-align: left;
}

.datasets__table thead {
  background: var(--el-fill-color-light);
}

.datasets__status--ok {
  color: var(--el-color-success);
}

.datasets__status--no {
  color: var(--el-text-color-secondary);
}
</style>