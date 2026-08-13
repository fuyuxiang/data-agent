<script setup lang="ts">
import type { Conversation } from '@/api/types'

defineProps<{ conversations: Conversation[]; activeId: number | null }>()
const emit = defineEmits<{ select: [number]; create: [] }>()
</script>

<template>
  <div class="list">
    <el-button
      class="list__new"
      data-test="new-conversation"
      size="small"
      @click="emit('create')"
    >
      + 新会话
    </el-button>
    <div
      v-for="item in conversations"
      :key="item.id"
      class="list__item"
      :class="{ 'list__item--active': item.id === activeId }"
      data-test="conversation-item"
      @click="emit('select', item.id)"
    >
      <div class="list__title">{{ item.title }}</div>
      <div class="list__meta">{{ item.dataset_name }}</div>
    </div>
    <div v-if="!conversations.length" class="list__empty">还没有会话</div>
  </div>
</template>

<style scoped>
.list {
  padding: 8px;
  font-size: 13px;
}

.list__new {
  width: 100%;
  margin-bottom: 8px;
}

.list__item {
  padding: 8px;
  border-radius: 4px;
  cursor: pointer;
}

.list__item:hover,
.list__item--active {
  background: var(--el-fill-color-light);
}

.list__title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.list__meta,
.list__empty {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>