<script setup lang="ts">
import type { Citation } from '@/api/types'

// Always expanded by design (spec 6): a number whose caliber is hidden is a
// number nobody trusts. There is deliberately no collapse prop.
defineProps<{ citation: Citation }>()
</script>

<template>
  <div class="citation" data-test="citation">
    <div class="citation__title">引证</div>
    <dl>
      <div class="citation__row">
        <dt>口径</dt>
        <dd>{{ citation.metric }}</dd>
      </div>
      <div class="citation__row">
        <dt>时间</dt>
        <dd>{{ citation.time }}</dd>
      </div>
      <div v-if="citation.filters.length" class="citation__row" data-test="citation-filters">
        <dt>过滤</dt>
        <dd>
          <div v-for="(line, index) in citation.filters" :key="index" class="citation__filter">
            <span>{{ line.label }} {{ line.value }}</span>
            <span v-if="line.source === 'permission'" class="citation__permission">
              ← 由数据权限自动附加
            </span>
          </div>
        </dd>
      </div>
      <div v-if="citation.data_updated_at" class="citation__row" data-test="citation-updated">
        <dt>更新</dt>
        <dd>{{ citation.data_updated_at }}</dd>
      </div>
    </dl>
  </div>
</template>

<style scoped>
.citation {
  margin-top: 12px;
  padding-top: 8px;
  border-top: 1px dashed var(--el-border-color);
  font-size: 13px;
  color: var(--el-text-color-regular);
}

.citation__title {
  margin-bottom: 6px;
  color: var(--el-text-color-secondary);
}

.citation__row {
  display: flex;
  gap: 8px;
  margin: 2px 0;
}

.citation__row dt {
  flex: none;
  width: 32px;
  color: var(--el-text-color-secondary);
}

.citation__row dd {
  margin: 0;
}

.citation__permission {
  margin-left: 8px;
  color: var(--el-color-warning);
}
</style>