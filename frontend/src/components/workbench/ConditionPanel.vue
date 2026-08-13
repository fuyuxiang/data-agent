<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'

import type { DatasetDetail, SlotState } from '@/api/types'

const props = defineProps<{
  slotState: SlotState | null
  dataset: DatasetDetail | null
  running: boolean
}>()
const emit = defineEmits<{ rerun: [SlotState] }>()

const COMPARISONS = [
  { value: 'none', label: '不对比' },
  { value: 'mom', label: '环比' },
  { value: 'yoy', label: '同比' },
  { value: 'ytd', label: '年累计' },
  { value: 'mtd', label: '月累计' },
]

function blank(): SlotState {
  return {
    kind: 'aggregate',
    dataset: '',
    metrics: [],
    dimensions: [],
    filters: [],
    time: null,
    comparison: 'none',
    sort: null,
    assumptions: [],
  }
}

function clone(value: SlotState): SlotState {
  return JSON.parse(JSON.stringify(value)) as SlotState
}

const draft = reactive<SlotState>(clone(props.slotState ?? blank()))
const dirty = ref(false)
let syncing = false

watch(
  () => props.slotState,
  (value) => {
    syncing = true
    Object.assign(draft, clone(value ?? blank()))
    dirty.value = false
    syncing = false
  },
  { deep: true }
)

watch(
  draft,
  () => {
    if (!syncing) dirty.value = true
  },
  { deep: true }
)

const metricOptions = computed(() =>
  (props.dataset?.metrics ?? []).map((metric) => ({
    value: metric.name,
    label: `${metric.business_name} v${metric.version}`,
    hint: metric.description,
  }))
)

const dimensionOptions = computed(() =>
  (props.dataset?.fields ?? [])
    .filter((field) => field.is_groupable)
    .map((field) => ({ value: field.name, label: field.business_name }))
)

const filterFieldOptions = computed(() =>
  (props.dataset?.fields ?? [])
    .filter((field) => field.is_filterable)
    .map((field) => ({ value: field.name, label: field.business_name }))
)

function enumOptions(fieldName: string) {
  const field = props.dataset?.fields.find((item) => item.name === fieldName)
  return (field?.enum_values ?? []).map((item) => ({
    value: item.business_value,
    label: item.business_value,
  }))
}

function addFilter(): void {
  draft.filters.push({ field: '', operator: 'in', values: [], spoken_values: [] })
}

function removeFilter(index: number): void {
  draft.filters.splice(index, 1)
}

function rerun(): void {
  emit('rerun', clone(draft))
}

const timeRange = computed({
  get: () => (draft.time ? [draft.time.start, draft.time.end] : []),
  set: (value: string[]) => {
    if (value && value.length === 2) {
      draft.time = {
        start: value[0]!,
        end: value[1]!,
        grain: draft.time?.grain ?? 'day',
        expression: draft.time?.expression ?? '',
      }
    } else {
      draft.time = null
    }
  },
})

defineExpose({
  draft,
  dirty,
  metricOptions,
  dimensionOptions,
  filterFieldOptions,
  enumOptions,
})
</script>

<template>
  <div class="panel">
    <div class="panel__title">当前查询条件</div>

    <div v-if="!slotState" class="panel__empty" data-test="panel-empty">
      提问后这里会显示可编辑的查询条件
    </div>

    <template v-else>
      <div class="panel__row">
        <label>指标</label>
        <el-select
          v-model="draft.metrics"
          multiple
          size="small"
          placeholder="选择指标"
        >
          <el-option
            v-for="item in metricOptions"
            :key="item.value"
            :value="item.value"
            :label="item.label"
          />
        </el-select>
      </div>

      <div class="panel__row">
        <label>时间</label>
        <el-date-picker
          v-if="draft.time"
          v-model="timeRange"
          type="daterange"
          size="small"
          value-format="YYYY-MM-DD"
          start-placeholder="开始"
          end-placeholder="结束"
        />
      </div>

      <div class="panel__row">
        <label>对比</label>
        <el-select v-model="draft.comparison" size="small">
          <el-option
            v-for="item in COMPARISONS"
            :key="item.value"
            :value="item.value"
            :label="item.label"
          />
        </el-select>
      </div>

      <div class="panel__row">
        <label>维度</label>
        <el-select v-model="draft.dimensions" multiple size="small" placeholder="不分组">
          <el-option
            v-for="item in dimensionOptions"
            :key="item.value"
            :value="item.value"
            :label="item.label"
          />
        </el-select>
      </div>

      <div class="panel__row panel__row--stack">
        <label>过滤</label>
        <div
          v-for="(filter, index) in draft.filters"
          :key="index"
          class="panel__filter"
        >
          <el-select v-model="filter.field" size="small" placeholder="字段">
            <el-option
              v-for="item in filterFieldOptions"
              :key="item.value"
              :value="item.value"
              :label="item.label"
            />
          </el-select>
          <el-select
            v-if="enumOptions(filter.field).length"
            v-model="filter.spoken_values"
            multiple
            size="small"
            placeholder="取值"
          >
            <el-option
              v-for="item in enumOptions(filter.field)"
              :key="item.value"
              :value="item.value"
              :label="item.label"
            />
          </el-select>
          <el-input
            v-else
            v-model="filter.spoken_values[0]"
            size="small"
            placeholder="取值"
          />
          <span class="panel__filter-values" :data-values="filter.spoken_values.join(',')">
            {{ filter.spoken_values.join('、') }}
          </span>
          <el-button text size="small" @click="removeFilter(index)">移除</el-button>
        </div>
        <el-button text size="small" @click="addFilter">+ 增加过滤</el-button>
      </div>

      <el-button
        class="panel__rerun"
        data-test="rerun"
        type="primary"
        size="small"
        :disabled="running"
        @click="rerun"
      >
        {{ dirty ? '按新条件重跑' : '重跑' }}
      </el-button>
    </template>
  </div>
</template>

<style scoped>
.panel {
  padding: 12px;
  font-size: 13px;
}

.panel__title {
  margin-bottom: 12px;
  font-weight: 600;
}

.panel__empty {
  color: var(--el-text-color-secondary);
}

.panel__row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.panel__row label {
  flex: none;
  width: 32px;
  color: var(--el-text-color-secondary);
}

.panel__row--stack {
  align-items: flex-start;
}

.panel__filter {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-bottom: 4px;
}

.panel__rerun {
  margin-top: 8px;
}
</style>