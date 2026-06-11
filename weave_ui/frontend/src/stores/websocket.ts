import { ref, watch } from "vue"
import { defineStore } from "pinia"
import { useWebSocket } from "../composables/useWebSocket"
import { useJobStore } from "./job"
import type { NodeStatus } from "../types"

export const useWebSocketStore = defineStore("websocket", () => {
  const connected = ref(false)
  const lastEventType = ref<string | null>(null)
  const dagNodeStatus = ref<Record<string, NodeStatus>>({})
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
      // Update DAG node status in real-time
      if (event.type === "execution_event") {
        const nodeId = event.node_id || event.payload?.node_id
        const eventType = event.event_type || event.payload?.event_type
        if (nodeId && eventType) {
          const statusMap: Record<string, NodeStatus> = {
            started: "running",
            completed: "success",
            failed: "failed",
            retrying: "retrying",
            skipped: "skipped",
          }
          const newStatus = statusMap[eventType]
          if (newStatus) dagNodeStatus.value[nodeId] = newStatus
        }
      }
      if (event.type === "dag_update") {
        dagNodeStatus.value = {}
      }
    }
    if (event.type === "session_end" || event.type === "session_start") {
      jobStore.fetchJobs()
    }
  }

  return { connected, lastEventType, dagNodeStatus, connect, disconnect, handleEvent }
})
