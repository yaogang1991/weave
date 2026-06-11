<template>
  <n-card size="small" hoverable style="cursor: pointer" @click="emit('select', job.id)">
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px">
      <n-text strong style="font-size: 13px; font-family: monospace">{{ job.id.slice(0, 16) }}</n-text>
      <StatusTag :status="job.status" />
    </div>
    <n-text depth="2" style="font-size: 13px">{{ truncatedReq }}</n-text>
    <div style="margin-top: 8px; display: flex; gap: 12px; font-size: 12px; color: #999">
      <span>{{ formatTime(job.created_at) }}</span>
      <span v-if="runtime">{{ runtime }}</span>
    </div>
  </n-card>
</template>
<script setup lang="ts">
import { computed } from 'vue'
import { NCard, NText } from 'naive-ui'
import StatusTag from './StatusTag.vue'
import type { Job } from '../types'
const props = defineProps<{ job: Job }>()
const emit = defineEmits<{ select: [id: string] }>()
const truncatedReq = computed(() => props.job.requirement.length > 80 ? props.job.requirement.slice(0, 80) + '...' : props.job.requirement)
const runtime = computed(() => {
  if (!props.job.updated_at || props.job.status === 'queued') return ''
  const ms = new Date(props.job.updated_at).getTime() - new Date(props.job.created_at).getTime()
  if (ms < 60000) return Math.round(ms / 1000) + 's'
  return Math.round(ms / 60000) + 'm'
})
function formatTime(iso: string | null) {
  if (!iso) return ''
  const d = new Date(iso)
  return d.toLocaleTimeString()
}
</script>
