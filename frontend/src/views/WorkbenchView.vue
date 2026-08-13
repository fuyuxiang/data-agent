<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { getDataset } from '@/api/semantic'
import AskInput from '@/components/workbench/AskInput.vue'
import ConditionPanel from '@/components/workbench/ConditionPanel.vue'
import ConversationList from '@/components/workbench/ConversationList.vue'
import MessageStream from '@/components/workbench/MessageStream.vue'
import ResultTable from '@/components/workbench/ResultTable.vue'
import { useSessionStore } from '@/stores/session'
import type {
  Clarification,
  ClarifyOption,
  DatasetDetail,
  DrillDown,
  SlotState,
} from '@/api/types'

const DATASET = 'orders'

const store = useSessionStore()
const dataset = ref<DatasetDetail | null>(null)

onMounted(async () => {
  await store.loadConversations()
  try {
    dataset.value = await getDataset(DATASET)
  } catch {
    dataset.value = null
  }
})

const latestAnswer = computed(() => {
  for (let index = store.messages.length - 1; index >= 0; index -= 1) {
    const message = store.messages[index]
    if (message.kind === 'answer' && message.answer) return message
  }
  return null
})

function onChoose(request: Clarification, option: ClarifyOption): void {
  void store.answerClarification(request, option)
}

function onDrill(item: DrillDown): void {
  if (!store.slotState) return
  const slots: SlotState = JSON.parse(JSON.stringify(store.slotState))
  if (item.kind === 'dimension' && !slots.dimensions.includes(item.target)) {
    slots.dimensions.push(item.target)
  }
  void store.rerunWithSlots(slots)
}
</script>

<template>
  <div class="workbench">
    <aside class="workbench__pane workbench__pane--left" data-test="pane-conversations">
      <ConversationList
        :conversations="store.conversations"
        :active-id="store.activeConversationId"
        @select="store.openConversation"
        @create="store.startNew"
      />
    </aside>

    <section class="workbench__pane workbench__pane--center" data-test="pane-chat">
      <div
        v-if="store.error"
        class="workbench__error"
        data-test="transport-error"
      >
        {{ store.error }}
      </div>
      <MessageStream
        class="workbench__stream"
        :messages="store.messages"
        :asking="store.asking"
        @choose="onChoose"
        @drill="onDrill"
      />
      <AskInput :disabled="store.asking" @submit="store.submit" />
    </section>

    <aside class="workbench__pane workbench__pane--right" data-test="pane-evidence">
      <ConditionPanel
        :slot-state="store.slotState"
        :dataset="dataset"
        :running="store.asking"
        @rerun="store.rerunWithSlots"
      />

      <div v-if="latestAnswer" class="workbench__evidence">
        <div class="workbench__subtitle">本轮成果</div>
        <ResultTable
          :columns="latestAnswer.answer!.columns"
          :rows="latestAnswer.answer!.rows"
        />
        <router-link
          class="workbench__trace"
          data-test="open-trace"
          :to="{ name: 'trace', params: { turnId: latestAnswer.turnId } }"
        >
          ▸ 查看 Trace
        </router-link>
      </div>
    </aside>
  </div>
</template>

<style scoped>
.workbench {
  display: grid;
  grid-template-columns: 200px 1fr 320px;
  height: 100vh;
}

.workbench__pane {
  overflow-y: auto;
}

.workbench__pane--left {
  border-right: 1px solid var(--el-border-color-light);
}

.workbench__pane--center {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.workbench__pane--right {
  border-left: 1px solid var(--el-border-color-light);
}

.workbench__stream {
  flex: 1;
  min-height: 0;
}

.workbench__error {
  margin: 12px;
  padding: 12px;
  background: var(--el-color-error-light-9);
  color: var(--el-color-error);
  border-radius: 4px;
  font-size: 13px;
}

.workbench__evidence {
  padding: 12px;
  border-top: 1px solid var(--el-border-color-light);
}

.workbench__subtitle {
  margin-bottom: 8px;
  font-size: 13px;
  font-weight: 600;
}

.workbench__trace {
  display: inline-block;
  margin-top: 8px;
  font-size: 13px;
}
</style>