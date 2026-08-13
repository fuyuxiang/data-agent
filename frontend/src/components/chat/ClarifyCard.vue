<script setup lang="ts">
import type { Clarification, ClarifyOption } from '@/api/types'

defineProps<{ clarifications: Clarification[] }>()
const emit = defineEmits<{ choose: [Clarification, ClarifyOption] }>()
</script>

<template>
  <div class="clarify">
    <div v-for="request in clarifications" :key="request.target" class="clarify__item">
      <div class="clarify__question">{{ request.question }}</div>
      <div v-if="request.options.length" class="clarify__options">
        <el-button
          v-for="option in request.options"
          :key="option.value"
          data-test="clarify-option"
          size="small"
          @click="emit('choose', request, option)"
        >
          {{ option.label }}
          <span v-if="option.hint" class="clarify__hint">（{{ option.hint }}）</span>
        </el-button>
      </div>
      <div v-else class="clarify__hint">请补充说明后重新提问</div>
    </div>
  </div>
</template>

<style scoped>
.clarify__item + .clarify__item {
  margin-top: 12px;
}

.clarify__question {
  margin-bottom: 8px;
}

.clarify__options {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.clarify__hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>