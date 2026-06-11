<template>
  <div>
    <n-space style="margin-bottom: 12px">
      <n-button @click="playing = !playing">{{ playing ? 'Pause' : 'Play' }}</n-button>
      <n-text depth="3">Event {{ currentIdx + 1 }} / {{ events.length }}</n-text>
    </n-space>
    <EventTimeline :events="visibleEvents" />
  </div>
</template>
<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { NButton, NSpace, NText } from 'naive-ui'
import EventTimeline from './EventTimeline.vue'
import type { SessionEvent } from '../types'
const props = defineProps<{ events: SessionEvent[] }>()
const playing = ref(false)
const currentIdx = ref(0)
const visibleEvents = computed(() => props.events.slice(0, currentIdx.value + 1))
let timer: ReturnType<typeof setInterval>
watch(playing, (v) => {
  clearInterval(timer)
  if (v) timer = setInterval(() => { if (currentIdx.value < props.events.length - 1) currentIdx.value++; else playing.value = false }, 500)
})
</script>
