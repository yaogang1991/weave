<template>
  <n-timeline v-if="events.length">
    <n-timeline-item v-for="(evt, i) in events" :key="i"
      :type="eventTypeColor(evt.event_type)" :title="formatLabel(evt.event_type)"
      :time="formatTime(evt.timestamp)">
      <template #icon>{{ eventIcon(evt.event_type) }}</template>
      <n-collapse v-if="evt.payload && Object.keys(evt.payload).length">
        <n-collapse-item title="Details">
          <pre style="font-size: 12px; white-space: pre-wrap; max-height: 200px; overflow: auto">{{ JSON.stringify(evt.payload, null, 2) }}</pre>
        </n-collapse-item>
      </n-collapse>
    </n-timeline-item>
  </n-timeline>
  <n-empty v-else description="No events" />
</template>
<script setup lang="ts">
import { NTimeline, NTimelineItem, NCollapse, NCollapseItem, NEmpty } from 'naive-ui'
import type { SessionEvent } from '../types'
defineProps<{ events: SessionEvent[] }>()
function eventIcon(t: string) {
  if (t.includes('tool_use')) return '🔧'
  if (t.includes('start')) return '▶️'
  if (t.includes('end')) return '✅'
  if (t.includes('error')) return '❌'
  if (t.includes('message')) return '💬'
  return '📌'
}
function eventTypeColor(t: string) {
  if (t.includes('error')) return 'error'
  if (t.includes('end') || t.includes('succeeded')) return 'success'
  if (t.includes('start')) return 'info'
  return 'default'
}
function formatLabel(t: string) { return t.replace(/\./g, ' › ') }
function formatTime(iso: string) { return new Date(iso).toLocaleTimeString() }
</script>
