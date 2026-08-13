<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import { ApiError } from '@/api/client'
import { getTrace, replayTurn } from '@/api/trace'
import StageDetail from '@/components/trace/StageDetail.vue'
import StageTimeline from '@/components/trace/StageTimeline.vue'
import type { Replay, Trace } from '@/api/types'

const STATUS_LABELS: Record<string, string> = {
  answered: '已作答',
  clarifying: '待澄清',
  refused: '已拒答',
  failed: '执行失败',
}

const route = useRoute()
const turnId = Number(route.params.turnId)

const trace = ref<Trace | null>(null)
const activeSequence = ref(1)
const replay = ref<Replay | null>(null)
const error = ref('')

onMounted(async () => {
  try {
    trace.value = await getTrace(turnId)
    activeSequence.value = trace.value.stages[0]?.sequence ?? 1
  } catch (raised) {
    error.value = raised instanceof ApiError ? raised.message : '加载失败'
  }
})

const activeStage = computed(
  () => trace.value?.stages.find((item) => item.sequence === activeSequence.value) ?? null
)

async function runReplay(): Promise<void> {
  error.value = ''
  replay.value = null
  try {
    replay.value = await replayTurn(turnId)
  } catch (raised) {
    error.value = raised instanceof ApiError ? raised.message : '重放失败'
  }
}

defineExpose({ activeSequence })
</script>

<template>
  <div class="trace">
    <header class="trace__header">
      <router-link to="/ask">← 返回工作台</router-link>
      <template v-if="trace">
        <h2>{{ trace.question }}</h2>
        <el-tag size="small" data-test="status-tag">
          {{ STATUS_LABELS[trace.status] ?? trace.status }}
        </el-tag>
      </template>
    </header>

    <div v-if="error" class="trace__error" data-test="trace-error">{{ error }}</div>

    <div v-if="trace" class="trace__body">
      <div class="trace__timeline">
        <StageTimeline
          :stages="trace.stages"
          :active-sequence="activeSequence"
          @select="(value) => (activeSequence = value)"
        />
        <el-button class="trace__replay" data-test="replay" size="small" @click="runReplay">
          从意图快照重放
        </el-button>
        <div v-if="replay" class="trace__replay-result" data-test="replay-result">
          <el-tag :type="replay.matches_original ? 'success' : 'warning'" size="small">
            {{ replay.matches_original ? '与原始一致' : '与原始不一致' }}
          </el-tag>
          <pre>{{ replay.display_sql }}</pre>
          <div v-if="replay.applied_row_filters.length" class="trace__note">
            行级权限附加：{{ replay.applied_row_filters.join('、') }}
          </div>
        </div>
      </div>
      <div class="trace__detail">
        <StageDetail :stage="activeStage" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.trace {
  padding: 16px;
}

.trace__header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}

.trace__header h2 {
  margin: 0;
  font-size: 16px;
}

.trace__error {
  padding: 12px;
  background: var(--el-color-error-light-9);
  color: var(--el-color-error);
  border-radius: 4px;
}

.trace__body {
  display: grid;
  grid-template-columns: 320px 1fr;
  gap: 16px;
  align-items: start;
}

.trace__replay {
  margin-top: 12px;
  width: 100%;
}

.trace__replay-result {
  margin-top: 8px;
}

.trace__replay-result pre {
  margin: 6px 0 0;
  padding: 8px;
  border-radius: 4px;
  background: var(--el-fill-color-light);
  font-size: 12px;
  white-space: pre-wrap;
}

.trace__note {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>