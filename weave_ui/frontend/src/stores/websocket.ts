import { ref, watch } from "vue"
import { defineStore } from "pinia"
import { useWebSocket } from "../composables/useWebSocket"
import { useJobStore } from "./job"

export const useWebSocketStore = defineStore("websocket", () => {
  const connected = ref(false)
  const lastEventType = ref<string | null>(null)
  let wsHandle: ReturnType<typeof useWebSocket> | null = null
  let stopWatchConn: (() => void) | null = null
  let stopWatchEvent: (() => void) | null = null

  function connect() {
    // Clean up previous connection if any
    disconnect()

    const wsProtocol = location.protocol === "https:" ? "wss:" : "ws:"
    const wsUrl = wsProtocol + "//" + location.host + "/ws"
    wsHandle = useWebSocket(wsUrl)

    stopWatchConn = watch(
      wsHandle.connected,
      (val) => { connected.value = val },
      { immediate: true }
    )
    stopWatchEvent = watch(
      wsHandle.lastEvent,
      (evt) => { if (evt) handleEvent(evt) }
    )
  }

  function disconnect() {
    stopWatchConn?.()
    stopWatchEvent?.()
    stopWatchConn = null
    stopWatchEvent = null
    wsHandle?.disconnect()
    wsHandle = null
    connected.value = false
  }

  function handleEvent(event: any) {
    if (!event) return
    lastEventType.value = event.type || event.event_type
    const jobStore = useJobStore()
    if (event.type === "execution_event" || event.type === "dag_update") {
      const jobId = event.job_id || event.payload?.job_id
      if (jobId) {
        const status = event.payload?.status || event.status
        if (status) jobStore.updateJobStatus(jobId, status)
      }
    }
    if (event.type === "session_end" || event.type === "session_start") {
      jobStore.fetchJobs()
    }
  }

  return { connected, lastEventType, connect, disconnect, handleEvent }
})
