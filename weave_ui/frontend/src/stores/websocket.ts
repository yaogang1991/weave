import { ref } from 'vue'
import { defineStore } from 'pinia'
import { useWebSocket } from '../composables/useWebSocket'
import { useJobStore } from './job'

export const useWebSocketStore = defineStore('websocket', () => {
  const connected = ref(false)
  const lastEventType = ref<string | null>(null)
  let ws: ReturnType<typeof useWebSocket> | null = null

  function connect() {
    const wsProtocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const wsUrl = wsProtocol + '//' + location.host + '/ws'
    const { connected: conn, lastEvent } = useWebSocket(wsUrl)
    ws = { connected: conn, lastEvent } as any
    // Watch for connection status
    connected.value = true
  }

  function handleEvent(event: any) {
    if (!event) return
    lastEventType.value = event.type || event.event_type
    const jobStore = useJobStore()

    if (event.type === 'execution_event' || event.type === 'dag_update') {
      const jobId = event.job_id || event.payload?.job_id
      if (jobId) {
        const status = event.payload?.status || event.status
        if (status) jobStore.updateJobStatus(jobId, status)
      }
    }
    if (event.type === 'session_end' || event.type === 'session_start') {
      jobStore.fetchJobs()
    }
  }

  return { connected, lastEventType, connect, handleEvent }
})
