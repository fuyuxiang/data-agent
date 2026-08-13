<script setup lang="ts">
import type { TraceStage } from '@/api/types'

const STAGE_LABELS: Record<string, string> = {
  verified_recall: '固定查询召回',
  intent: '意图识别',
  semantic_resolve: '语义解析',
  compile: 'SQL 编译',
  security: '安全改写',
  execute: '执行与校验',
  answer: '作答',
}

defineProps<{ stages: TraceStage[]; activeSequence: number }>()
const emit = defineEmits<{ select: [number] }>()

function label(name: string): string {
  return STAGE_LABELS[name] ?? name
}
</script>

<template>
  <ol class="timeline">
    <li
      v-for="item in stages"
      :key="item.sequence"
      class="stage"
      :class="{
        'stage--error': Boolean(item.error),
        'stage--active': item.sequence === activeSequence,
      }"
      data-test="stage-item"
      @click="emit('select', item.sequence)"
    >
      <div class="stage__name">{{ item.sequence }}. {{ label(item.stage) }}</div>
      <div class="stage__meta">
        <span>{{ item.elapsed_ms }} ms</span>
        <span v-if="item.model">
          {{ item.model }} · Token {{ item.prompt_tokens }}/{{ item.completion_tokens }}
        </span>
      </div>
      <div v-if="item.error" class="stage__error-text">{{ item.error }}</div>
    </li>
  </ol>
</template>

<style scoped>
.timeline {
  margin: 0;
  padding: 0;
  list-style: none;
}

.stage {
  padding: 8px 10px;
  border-left: 3px solid var(--el-border-color);
  cursor: pointer;
  font-size: 13px;
}

.stage--active {
  background: var(--el-fill-color-light);
  border-left-color: var(--el-color-primary);
}

.stage--error {
  border-left-color: var(--el-color-danger);
}

.stage__meta {
  display: flex;
  gap: 10px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.stage__error-text {
  margin-top: 4px;
  color: var(--el-color-danger);
  font-size: 12px;
}
</style>