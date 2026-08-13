<script setup lang="ts">
import { computed } from 'vue'

import type { LintReport } from '@/api/types'

const props = defineProps<{ report: LintReport | null; publishing: boolean }>()
const emit = defineEmits<{ publish: [] }>()

const errors = computed(
  () => props.report?.issues.filter((item) => item.severity === 'error') ?? []
)
const warnings = computed(
  () => props.report?.issues.filter((item) => item.severity === 'warning') ?? []
)
</script>

<template>
  <div class="lint">
    <div class="lint__title">语义体检</div>
    <div v-if="!report" class="lint__empty">体检结果加载中</div>
    <template v-else>
      <div
        v-if="!errors.length && !warnings.length"
        class="lint__ok"
        data-test="lint-ok"
      >
        ✓ 未发现问题
      </div>
      <div
        v-for="(issue, index) in errors"
        :key="`e${index}`"
        class="lint__issue lint__issue--error"
        data-test="lint-issue"
      >
        <strong>错误</strong> {{ issue.target }}：{{ issue.message }}
      </div>
      <div
        v-for="(issue, index) in warnings"
        :key="`w${index}`"
        class="lint__issue lint__issue--warning"
        data-test="lint-warning"
      >
        <strong>警告</strong> {{ issue.target }}：{{ issue.message }}
      </div>
      <el-button
        class="lint__publish"
        data-test="publish"
        type="primary"
        size="small"
        :disabled="!report.publishable || publishing"
        @click="emit('publish')"
      >
        发布
      </el-button>
      <div v-if="!report.publishable" class="lint__hint">
        存在错误项，修复后才能发布
      </div>
    </template>
  </div>
</template>

<style scoped>
.lint {
  font-size: 13px;
}

.lint__title {
  margin-bottom: 8px;
  font-weight: 600;
}

.lint__issue {
  margin-bottom: 6px;
  padding: 8px;
  border-radius: 4px;
  font-size: 12px;
}

.lint__issue--error {
  background: var(--el-color-error-light-9);
  color: var(--el-color-error);
}

.lint__issue--warning {
  background: var(--el-color-warning-light-9);
  color: var(--el-color-warning);
}

.lint__ok {
  color: var(--el-color-success);
  font-size: 13px;
}

.lint__publish {
  margin-top: 8px;
}

.lint__empty,
.lint__hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>