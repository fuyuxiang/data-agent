<script setup lang="ts">
import { ref } from 'vue'

import { sendFeedback } from '@/api/chat'
import { ApiError } from '@/api/client'
import type { FeedbackCategory } from '@/api/types'

const props = defineProps<{ turnId: number }>()

const categories: { value: FeedbackCategory; label: string }[] = [
  { value: 'metric', label: '指标错' },
  { value: 'time', label: '时间错' },
  { value: 'sql', label: 'SQL 错' },
  { value: 'calculation', label: '计算错' },
  { value: 'conclusion', label: '结论错' },
]

const dialogVisible = ref(false)
const submitted = ref(false)
const category = ref<FeedbackCategory | ''>('')
const comment = ref('')
const hint = ref('')

async function thumbUp(): Promise<void> {
  try {
    await sendFeedback(props.turnId, { is_positive: true })
    submitted.value = true
  } catch (raised) {
    hint.value = raised instanceof ApiError ? raised.message : '提交失败'
  }
}

function thumbDown(): void {
  hint.value = ''
  dialogVisible.value = true
}

async function confirm(): Promise<void> {
  if (!category.value) {
    hint.value = '请选择一个归因分类'
    return
  }
  try {
    await sendFeedback(props.turnId, {
      is_positive: false,
      category: category.value,
      comment: comment.value,
    })
    submitted.value = true
    dialogVisible.value = false
  } catch (raised) {
    hint.value = raised instanceof ApiError ? raised.message : '提交失败'
  }
}

defineExpose({ dialogVisible, categories, category, comment, hint, confirm })
</script>

<template>
  <div class="feedback" data-test="feedback">
    <el-button
      data-test="thumb-up"
      text
      size="small"
      :disabled="submitted"
      @click="thumbUp"
    >
      👍
    </el-button>
    <el-button
      data-test="thumb-down"
      text
      size="small"
      :disabled="submitted"
      @click="thumbDown"
    >
      👎
    </el-button>
    <span v-if="submitted" class="feedback__done">已记录，谢谢</span>

    <el-dialog v-model="dialogVisible" title="哪里不对？" width="420px">
      <el-radio-group v-model="category">
        <el-radio v-for="item in categories" :key="item.value" :value="item.value">
          {{ item.label }}
        </el-radio>
      </el-radio-group>
      <el-input
        v-model="comment"
        class="feedback__comment"
        type="textarea"
        :rows="3"
        placeholder="补充说明（选填）"
      />
      <div v-if="hint" class="feedback__hint">{{ hint }}</div>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="confirm">提交</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.feedback {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: 8px;
}

.feedback__done,
.feedback__hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.feedback__hint {
  margin-top: 8px;
  color: var(--el-color-danger);
}

.feedback__comment {
  margin-top: 12px;
}
</style>