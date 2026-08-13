<script setup lang="ts">
import { computed } from 'vue'

import type { TraceStage } from '@/api/types'

const props = defineProps<{ stage: TraceStage | null }>()

const sql = computed(() => {
  const value = props.stage?.output_payload?.sql
  return typeof value === 'string' ? value : ''
})

function pretty(payload: Record<string, unknown> | null): string {
  return payload ? JSON.stringify(payload, null, 2) : '—'
}
</script>

<template>
  <div class="detail" data-test="stage-detail">
    <div v-if="!stage" class="detail__empty">选择左侧的阶段查看详情</div>
    <template v-else>
      <div v-if="sql" class="detail__block">
        <div class="detail__title">实际执行的 SQL</div>
        <pre data-test="stage-sql">{{ sql }}</pre>
      </div>
      <div class="detail__block">
        <div class="detail__title">输入</div>
        <pre>{{ pretty(stage.input_payload) }}</pre>
      </div>
      <div class="detail__block">
        <div class="detail__title">输出</div>
        <pre>{{ pretty(stage.output_payload) }}</pre>
      </div>
    </template>
  </div>
</template>

<style scoped>
.detail {
  font-size: 13px;
}

.detail__empty {
  color: var(--el-text-color-secondary);
}

.detail__block + .detail__block {
  margin-top: 12px;
}

.detail__title {
  margin-bottom: 4px;
  color: var(--el-text-color-secondary);
}

pre {
  margin: 0;
  padding: 8px;
  border-radius: 4px;
  background: var(--el-fill-color-light);
  overflow-x: auto;
  font-size: 12px;
}
</style>