<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import { ApiError } from '@/api/client'
import { getDataset, getLint, publishDataset } from '@/api/semantic'
import LintReportCard from '@/components/admin/LintReportCard.vue'
import type { DatasetDetail, LintReport } from '@/api/types'

const route = useRoute()
const name = String(route.params.name)

const dataset = ref<DatasetDetail | null>(null)
const report = ref<LintReport | null>(null)
const error = ref('')
const publishing = ref(false)

async function load(): Promise<void> {
  try {
    dataset.value = await getDataset(name)
    report.value = await getLint(name)
  } catch (raised) {
    error.value = raised instanceof ApiError ? raised.message : '加载失败'
  }
}

onMounted(load)

async function publish(): Promise<void> {
  error.value = ''
  publishing.value = true
  try {
    await publishDataset(name)
    await load()
  } catch (raised) {
    error.value = raised instanceof ApiError ? raised.message : '发布失败'
  } finally {
    publishing.value = false
  }
}

function enumSummary(values: { business_value: string; aliases: string[] }[]): string {
  return values
    .map((item) =>
      item.aliases.length
        ? `${item.business_value}（${item.aliases.join('、')}）`
        : item.business_value
    )
    .join('，')
}
</script>

<template>
  <div class="dataset">
    <header class="dataset__header">
      <router-link to="/admin/datasets">← 数据集列表</router-link>
      <template v-if="dataset">
        <h2>{{ dataset.business_name }}（{{ dataset.name }}）</h2>
        <span
          :class="[
            'dataset__status',
            dataset.is_published ? 'dataset__status--ok' : 'dataset__status--no',
          ]"
        >
          {{ dataset.is_published ? '已发布' : '未发布' }}
        </span>
      </template>
    </header>

    <div v-if="error" class="dataset__error" data-test="dataset-error">{{ error }}</div>

    <div v-if="dataset" class="dataset__body">
      <section class="dataset__main">
        <dl class="dataset__meta">
          <div><dt>物理表</dt><dd>{{ dataset.physical_table }}</dd></div>
          <div><dt>粒度</dt><dd>{{ dataset.grain }}</dd></div>
          <div><dt>适用</dt><dd>{{ dataset.applicable_scenario }}</dd></div>
          <div data-test="forbidden">
            <dt>禁用</dt>
            <dd class="dataset__forbidden">{{ dataset.forbidden_scenario }}</dd>
          </div>
        </dl>

        <h3>指标</h3>
        <table class="dataset__table" data-test="metrics">
          <thead>
            <tr>
              <th>名称</th>
              <th>标识</th>
              <th>版本</th>
              <th>类型</th>
              <th>聚合行为</th>
              <th>口径说明</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="metric in dataset.metrics" :key="metric.name">
              <td>{{ metric.business_name }}</td>
              <td>{{ metric.name }}</td>
              <td>v{{ metric.version }}</td>
              <td>{{ metric.kind }}</td>
              <td>{{ metric.aggregation_behavior }}</td>
              <td>{{ metric.description }}</td>
            </tr>
          </tbody>
        </table>

        <h3>字段</h3>
        <table class="dataset__table" data-test="fields">
          <thead>
            <tr>
              <th>名称</th>
              <th>标识</th>
              <th>语义类型</th>
              <th>默认聚合</th>
              <th>允许聚合</th>
              <th>敏感级</th>
              <th>枚举值</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="field in dataset.fields" :key="field.name">
              <td>{{ field.business_name }}</td>
              <td>{{ field.name }}</td>
              <td>{{ field.semantic_type }}</td>
              <td>{{ field.default_aggregation }}</td>
              <td>{{ field.allowed_aggregations.join('、') || '—' }}</td>
              <td>{{ field.sensitivity }}</td>
              <td>{{ enumSummary(field.enum_values) || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <aside class="dataset__side">
        <LintReportCard :report="report" :publishing="publishing" @publish="publish" />
      </aside>
    </div>
  </div>
</template>

<style scoped>
.dataset {
  padding: 16px;
}

.dataset__header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}

.dataset__header h2 {
  margin: 0;
  font-size: 16px;
}

.dataset__status--ok {
  color: var(--el-color-success);
}

.dataset__status--no {
  color: var(--el-text-color-secondary);
}

.dataset__error {
  padding: 12px;
  background: var(--el-color-error-light-9);
  color: var(--el-color-error);
  border-radius: 4px;
}

.dataset__body {
  display: grid;
  grid-template-columns: 1fr 320px;
  gap: 16px;
  align-items: start;
}

.dataset__meta {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 16px;
  margin: 0 0 12px;
}

.dataset__meta div {
  display: flex;
  gap: 8px;
}

.dataset__meta dt {
  flex: none;
  width: 56px;
  color: var(--el-text-color-secondary);
}

.dataset__meta dd {
  margin: 0;
}

.dataset__forbidden {
  color: var(--el-color-warning);
}

.dataset__table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  margin-bottom: 16px;
}

.dataset__table th,
.dataset__table td {
  padding: 6px 8px;
  border: 1px solid var(--el-border-color-light);
  text-align: left;
}

.dataset__table thead {
  background: var(--el-fill-color-light);
}

h3 {
  margin: 12px 0 8px;
  font-size: 14px;
}
</style>