<template>
  <n-tag :type="tagType" size="small" round>{{ status }}</n-tag>
</template>
<script setup lang="ts">
import { computed } from 'vue'
import { NTag } from 'naive-ui'
const props = defineProps<{ status: string }>()
const tagType = computed(() => {
  const map: Record<string, string> = {
    running: 'info', leased: 'info', queued: 'default',
    succeeded: 'success', failed: 'error', dead_letter: 'error',
    canceled: 'warning', pending_approval: 'warning',
  }
  return (map[props.status] || 'default') as any
})
</script>
