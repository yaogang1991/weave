import { ref } from 'vue'

export function useWebSocket(url: string) {
  const connected = ref(false)
  const lastEvent = ref<any>(null)
  let ws: WebSocket | null = null
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null

  function connect() {
    // Clean up any existing connection
    _close()
    ws = new WebSocket(url)
    ws.onopen = () => { connected.value = true }
    ws.onmessage = (e) => { try { lastEvent.value = JSON.parse(e.data) } catch { lastEvent.value = e.data } }
    ws.onclose = () => { connected.value = false; reconnectTimer = setTimeout(connect, 5000) }
    ws.onerror = () => { ws?.close() }
  }

  function _close() {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
    if (ws) { ws.onclose = null; ws.close(); ws = null }
    connected.value = false
  }

  function disconnect() { _close() }
  function send(data: object) { if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(data)) }

  // Auto-connect on creation
  connect()

  return { connected, lastEvent, send, reconnect: connect, disconnect }
}
