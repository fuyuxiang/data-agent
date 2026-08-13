<script setup lang="ts">
import CitationBlock from '@/components/chat/CitationBlock.vue'
import ClarifyCard from '@/components/chat/ClarifyCard.vue'
import FeedbackBar from '@/components/chat/FeedbackBar.vue'
import type { Clarification, ClarifyOption, DrillDown } from '@/api/types'
import type { ChatMessage } from '@/stores/session'

defineProps<{ message: ChatMessage }>()
const emit = defineEmits<{
  choose: [Clarification, ClarifyOption]
  drill: [DrillDown]
}>()
</script>

<template>
  <div class="answer-card">
    <template v-if="message.kind === 'answer' && message.answer">
      <div class="answer-card__headline" data-test="headline">
        {{ message.answer.headline }}
      </div>
      <div v-if="message.answer.conclusion" class="answer-card__conclusion">
        {{ message.answer.conclusion }}
      </div>
      <div
        v-if="message.answer.assumptions.length"
        class="answer-card__assumptions"
        data-test="assumptions"
      >
        <strong>默认假设：</strong>
        <ul>
          <li v-for="item in message.answer.assumptions" :key="item">{{ item }}</li>
        </ul>
      </div>
      <div
        v-for="warning in message.answer.warnings"
        :key="warning"
        class="answer-card__warning"
      >
        ⚠ {{ warning }}
      </div>
      <div v-if="message.answer.rows.length" class="answer-card__table">
        <el-table :data="message.answer.rows" stripe size="small" border>
          <el-table-column
            v-for="(col, index) in message.answer.columns"
            :key="col"
            :label="col"
            :prop="String(index)"
          />
        </el-table>
      </div>
      <CitationBlock v-if="message.answer.citation" :citation="message.answer.citation" />
      <div
        v-if="message.answer.drill_downs.length"
        class="answer-card__drill-downs"
      >
        <el-button
          v-for="item in message.answer.drill_downs"
          :key="item.target"
          data-test="drill-down"
          size="small"
          text
          @click="emit('drill', item)"
        >
          {{ item.label }}
        </el-button>
      </div>
      <FeedbackBar :turn-id="message.turnId" />
      <router-link
        :to="{ name: 'trace', params: { turnId: String(message.turnId) } }"
        class="answer-card__trace-link"
        data-test="trace-link"
      >
        查看 Trace
      </router-link>
    </template>

    <template v-else-if="message.kind === 'clarify'">
      <ClarifyCard
        :clarifications="message.clarifications ?? []"
        @choose="(req, opt) => emit('choose', req, opt)"
      />
    </template>

    <template v-else>
      <div class="answer-card__refusal" data-test="refusal">{{ message.reason }}</div>
      <router-link
        :to="{ name: 'trace', params: { turnId: String(message.turnId) } }"
        class="answer-card__trace-link"
        data-test="trace-link"
      >
        查看 Trace
      </router-link>
    </template>
  </div>
</template>

<style scoped>
.answer-card {
  display: flex;
  flex-direction: column;
}

.answer-card__headline {
  font-size: 16px;
  font-weight: 600;
  margin-bottom: 6px;
}

.answer-card__conclusion {
  margin-bottom: 6px;
}

.answer-card__assumptions,
.answer-card__warning,
.answer-card__refusal {
  font-size: 13px;
  margin: 4px 0;
}

.answer-card__warning {
  color: var(--el-color-warning);
}

.answer-card__refusal {
  color: var(--el-text-color-regular);
}

.answer-card__drill-downs {
  margin-top: 8px;
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

.answer-card__trace-link {
  margin-top: 8px;
  font-size: 12px;
  color: var(--el-color-primary);
}
</style>