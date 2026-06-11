import { ref, watch } from 'vue'
import { defineStore } from 'pinia'
import { useWebSocket } from '../composables/useWebSocket'
import { useJobStore } from './job'

export const useWebSocketStore = defineStore('websocket', () => {
  const connected = ref(false)
  const lastEventType = ref<string | null>(null)
  let cleanup: (() => void) | null = null

  function connect() {
    if (cleanup) return // already connected
    const wsProtocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const wsUrl = wsProtocol + '//' + location.host + '/ws'
    const { connected: conn, lastEvent } = useWebSocket(wsUrl)

    // Fix #6: reflect actual connection state instead of assuming true
    watch(conn, (val) => { connected.value = val }, { immediate: true })

    // Fix #7: wire handleEvent to incoming WebSocket messages
    watch(lastEvent, (evt) => { if (evt) handleEvent(evt) })

    cleanup = () => { conn.value = false }
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
}
