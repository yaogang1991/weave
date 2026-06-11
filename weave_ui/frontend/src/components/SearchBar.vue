<template>
  <n-space>
    <n-input v-model:value="query" placeholder="Search tasks..." clearable style="width: 300px" @input="onInput" />
    <n-select v-model:value="statusFilter" :options="statusOptions" clearable placeholder="Status" style="width: 140px" />
  </n-space>
</template>
<script setup lang="ts">
import { ref } from 'vue'
import { NInput, NSelect, NSpace } from 'naive-ui'
const query = ref('')
const statusFilter = ref<string | null>(null)
const statusOptions = [
  { label: 'Succeeded', value: 'succeeded' },
  { label: 'Failed', value: 'failed' },
  { label: 'Canceled', value: 'canceled' },
]
let timer: ReturnType<typeof setTimeout>
function onInput() {
  clearTimeout(timer)
  timer = setTimeout(() => emit('search', { q: query.value, status: statusFilter.value }), 300)
}
const emit = defineEmits<{ search: [params: { q: string; status: string | null }] }>()
</script>
