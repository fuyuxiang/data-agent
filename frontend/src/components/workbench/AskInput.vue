<script setup lang="ts">
import { ref } from 'vue'

const props = defineProps<{ disabled: boolean }>()
const emit = defineEmits<{ submit: [string] }>()

const text = ref('')

function submit(): void {
  if (props.disabled || !text.value.trim()) return
  emit('submit', text.value)
  text.value = ''
}
</script>

<template>
  <div class="ask" data-test="ask-input">
    <textarea
      v-model="text"
      class="ask__textarea"
      rows="2"
      placeholder="问一个数据问题，例如：华东本月销售额环比"
      @keydown.enter.exact.prevent="submit"
    />
    <el-button
      data-test="ask-submit"
      type="primary"
      :disabled="disabled"
      @click="submit"
    >
      提问
    </el-button>
  </div>
</template>

<style scoped>
.ask__textarea {
  flex: 1;
  resize: none;
  padding: 8px;
  font: inherit;
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
}
</style>

<style scoped>
.ask {
  display: flex;
  gap: 8px;
  padding: 12px;
  border-top: 1px solid var(--el-border-color-light);
}
</style>