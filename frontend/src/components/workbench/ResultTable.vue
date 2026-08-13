<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ columns: string[]; rows: unknown[][] }>()

const NULL_PLACEHOLDER = '—'

function cellValue(row: unknown[], index: number): unknown {
  const value = row[index]
  return value === null || value === undefined ? NULL_PLACEHOLDER : value
}

const tableRows = computed(() =>
  props.rows.map((row) => {
    const record: Record<string, unknown> = {}
    props.columns.forEach((column, index) => {
      record[column] = cellValue(row, index)
    })
    return record
  })
)

const tableColumns = computed(() =>
  props.columns.map((column) => ({ label: column, prop: column }))
)

function escapeCsv(value: unknown): string {
  if (value === null || value === undefined) return ''
  const text = String(value)
  if (text.includes(',') || text.includes('"') || text.includes('\n')) {
    return `"${text.replace(/"/g, '""')}"`
  }
  return text
}

function toCsv(): string {
  const header = props.columns.map(escapeCsv).join(',')
  const body = props.rows.map((row) => row.map(escapeCsv).join(',')).join('\n')
  return body ? `${header}\n${body}` : header
}

function downloadCsv(): void {
  const blob = new Blob([toCsv()], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = 'result.csv'
  link.click()
  URL.revokeObjectURL(url)
}

defineExpose({ tableColumns, tableRows, toCsv })
</script>

<template>
  <div class="result">
    <div v-if="!columns.length" class="result__empty" data-test="result-empty">
      暂无数据
    </div>
    <template v-else>
      <div class="result__actions">
        <el-button text size="small" data-test="csv-download" @click="downloadCsv">
          导出 CSV
        </el-button>
      </div>
      <table class="result__table" data-test="result-table">
        <thead>
          <tr>
            <th v-for="column in columns" :key="column" data-test="result-column">
              {{ column }}
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, rowIndex) in tableRows" :key="rowIndex" data-test="result-row">
            <td v-for="column in columns" :key="column">{{ row[column] }}</td>
          </tr>
        </tbody>
      </table>
    </template>
  </div>
</template>

<style scoped>
.result__empty {
  color: var(--el-text-color-secondary);
  padding: 24px;
  text-align: center;
}

.result__actions {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 4px;
}

.result__table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.result__table th,
.result__table td {
  padding: 6px 8px;
  border: 1px solid var(--el-border-color-light);
  text-align: left;
}

.result__table thead {
  background: var(--el-fill-color-light);
}
</style>