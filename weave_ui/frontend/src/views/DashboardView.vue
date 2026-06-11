<template>
  <div>
    <n-h1>Dashboard</n-h1>
    <n-space vertical size="large">
      <n-grid :cols="4" :x-gap="12" :y-gap="12">
        <n-gi><n-statistic label="Running" :value="jobStore.runningJobs.length" /></n-gi>
        <n-gi><n-statistic label="Queued" :value="jobStore.queuedJobs.length" /></n-gi>
        <n-gi><n-statistic label="Failed" :value="jobStore.failedJobs.length" /></n-gi>
        <n-gi><n-statistic label="Succeeded" :value="jobStore.succeededJobs.length" /></n-gi>
      </n-grid>
      <template v-if="jobStore.runningJobs.length">
        <n-h2 prefix="bar" type="info">Running</n-h2>
        <n-grid :cols="2" :x-gap="12" :y-gap="12">
          <n-gi v-for="j in jobStore.runningJobs" :key="j.id"><JobCard :job="j" @select="goJob" /></n-gi>
        </n-grid>
      </template>
      <template v-if="jobStore.queuedJobs.length">
        <n-h2 prefix="bar">Queued</n-h2>
        <n-grid :cols="2" :x-gap="12" :y-gap="12">
          <n-gi v-for="j in jobStore.queuedJobs" :key="j.id"><JobCard :job="j" @select="goJob" /></n-gi>
        </n-grid>
      </template>
      <template v-if="jobStore.failedJobs.length">
        <n-h2 prefix="bar" type="error">Failed</n-h2>
        <n-grid :cols="2" :x-gap="12" :y-gap="12">
          <n-gi v-for="j in jobStore.failedJobs" :key="j.id"><JobCard :job="j" @select="goJob" /></n-gi>
        </n-grid>
      </template>
      <template v-if="jobStore.succeededJobs.length">
        <n-h2 prefix="bar" type="success">Succeeded</n-h2>
        <n-grid :cols="2" :x-gap="12" :y-gap="12">
          <n-gi v-for="j in jobStore.succeededJobs" :key="j.id"><JobCard :job="j" @select="goJob" /></n-gi>
        </n-grid>
      </template>
      <n-empty v-if="!jobStore.jobs.length" description="No jobs yet. Submit a task to get started." />
    </n-space>
  </div>
</template>
<script setup lang="ts">
import { onMounted, onUnmounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { NH1, NH2, NGrid, NGi, NSpace, NStatistic, NEmpty } from 'naive-ui'
import { useJobStore } from '../stores/job'
import { useWebSocketStore } from '../stores/websocket'
import JobCard from '../components/JobCard.vue'
const jobStore = useJobStore()
const wsStore = useWebSocketStore()
const router = useRouter()
let timer: ReturnType<typeof setInterval> | null = null

function startPolling() {
  if (timer) return
  timer = setInterval(() => jobStore.fetchJobs(), 30000)
}

function stopPolling() {
  if (timer !== null) { clearInterval(timer); timer = null }
}

// Poll only when WebSocket is disconnected
watch(() => wsStore.connected, (isConnected) => {
  if (isConnected) stopPolling()
  else startPolling()
}, { immediate: true })

onMounted(() => {
  jobStore.fetchJobs()
  wsStore.connect()
  // Start polling as fallback if WS isn't connected yet
  if (!wsStore.connected) startPolling()
})
onUnmounted(() => stopPolling())
function goJob(id: string) { router.push('/jobs/' + id) }
</script>
