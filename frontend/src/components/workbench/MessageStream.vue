<script setup lang="ts">
import AnswerCard from '@/components/chat/AnswerCard.vue'
import ClarifyCard from '@/components/chat/ClarifyCard.vue'
import type { Clarification, ClarifyOption, DrillDown } from '@/api/types'
import type { ChatMessage } from '@/stores/session'

defineProps<{ messages: ChatMessage[]; asking: boolean }>()
const emit = defineEmits<{
  choose: [Clarification, ClarifyOption]
  drill: [DrillDown]
}>()
</script>

<template>
  <div class="stream">
    <div v-for="message in messages" :key="message.id" class="stream__row">
      <div v-if="message.role === 'user'" class="stream__question">
        {{ message.question }}
      </div>
      <div v-else class="stream__bubble">
        <ClarifyCard
          v-if="message.kind === 'clarify' && message.clarifications"
          :clarifications="message.clarifications"
          @choose="(request, option) => emit('choose', request, option)"
        />
        <AnswerCard v-else :message="message" @drill="(item) => emit('drill', item)" />
      </div>
    </div>
    <div v-if="asking" class="stream__pending">正在查询…</div>
  </div>
</template>

<style scoped>
.stream {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 16px;
  overflow-y: auto;
}

.stream__question {
  align-self: flex-end;
  padding: 8px 12px;
  border-radius: 6px;
  background: var(--el-color-primary-light-9);
}

.stream__bubble {
  padding: 12px;
  border: 1px solid var(--el-border-color-light);
  border-radius: 6px;
}

.stream__pending {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
</style>