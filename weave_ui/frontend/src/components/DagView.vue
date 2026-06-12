<template>
  <div v-if="dag" class="dag-view">
    <!-- Status summary -->
    <n-space style="margin-bottom: 16px" align="center">
      <n-tag :type="progressTagType" round>{{ progressLabel }}</n-tag>
      <n-text depth="3" style="font-size: 13px">
        {{ completedCount }} / {{ dag.nodes.length }} nodes
      </n-text>
    </n-space>

    <!-- Reasoning (collapsible) -->
    <n-collapse v-if="dag.reasoning" style="margin-bottom: 16px">
      <n-collapse-item title="Planning Reasoning" name="reasoning">
        <n-text style="white-space: pre-wrap; font-size: 13px">{{ dag.reasoning }}</n-text>
      </n-collapse-item>
    </n-collapse>

    <!-- DAG graph -->
    <div ref="containerRef" class="dag-container">
      <div class="dag-levels">
        <div v-for="(level, li) in dag.levels" :key="li" class="dag-level">
          <div
            v-for="nodeId in level" :key="nodeId"
            :ref="(el: any) => { if (el) nodeRefs[nodeId] = el }"
            class="dag-node"
            :class="'dag-node--' + getNode(nodeId).status"
          >
            <div class="dag-node__header">
              <span class="dag-node__icon">{{ agentIcon(getNode(nodeId).agent_type) }}</span>
              <span class="dag-node__id">{{ nodeId }}</span>
              <n-spin v-if="getNode(nodeId).status === 'running'" :size="'small'" />
              <span v-else class="dag-node__status">{{ statusIcon(getNode(nodeId).status) }}</span>
            </div>
            <div class="dag-node__task">{{ truncate(getNode(nodeId).task, 80) }}</div>
            <div v-if="getNode(nodeId).duration_ms != null" class="dag-node__duration">
              {{ formatDuration(getNode(nodeId).duration_ms!) }}
            </div>
            <div v-if="getNode(nodeId).error" class="dag-node__error">
              {{ truncate(getNode(nodeId).error!, 60) }}
            </div>
          </div>
        </div>
      </div>
      <!-- SVG edges overlay -->
      <svg class="dag-edges" :width="svgWidth" :height="svgHeight">
        <path
          v-for="(edge, ei) in dag.edges" :key="ei"
          :d="calcPath(edge)"
          :stroke="edgeColor(edge)"
          stroke-width="2"
          fill="none"
        />
      </svg>
    </div>
  </div>
  <n-empty v-else description="No DAG data available" />
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { NSpace, NTag, NText, NCollapse, NCollapseItem, NSpin, NEmpty } from 'naive-ui'
import { useWebSocketStore } from '../stores/websocket'
import type { DAGData, DAGNode, DAGEdge, NodeStatus } from '../types'

const props = defineProps<{ dag: DAGData | null }>()
const wsStore = useWebSocketStore()

const containerRef = ref<HTMLElement | null>(null)
const nodeRefs: Record<string, HTMLElement> = {}
const svgWidth = ref(0)
const svgHeight = ref(0)
let resizeObserver: ResizeObserver | null = null

// Merge real-time WebSocket status into DAG nodes
const liveDag = computed(() => {
  if (!props.dag) return null
  return {
    ...props.dag,
    nodes: props.dag.nodes.map(n => ({
      ...n,
      status: (wsStore.dagNodeStatus[n.id] as NodeStatus) || n.status,
    })),
  }
})

const dag = computed(() => liveDag.value || props.dag)

function getNode(id: string): DAGNode & { status: NodeStatus } {
  const node = dag.value?.nodes.find(n => n.id === id)
  return node || { id, agent_type: 'worker', task: '', status: 'pending' as NodeStatus, started_at: null, completed_at: null, error: null, duration_ms: null }
}

const completedCount = computed(() => {
  if (!dag.value) return 0
  return dag.value.nodes.filter(n => ['success', 'failed', 'skipped'].includes(getNode(n.id).status)).length
})

const progressLabel = computed(() => {
  if (!dag.value) return ''
  const statuses = dag.value.nodes.map(n => getNode(n.id).status)
  if (statuses.every(s => s === 'success')) return 'All completed'
  if (statuses.some(s => s === 'running')) return 'Executing...'
  if (statuses.some(s => s === 'failed')) return 'Has failures'
  return 'Planned'
})

const progressTagType = computed(() => {
  if (!dag.value) return 'default' as const
  const statuses = dag.value.nodes.map(n => getNode(n.id).status)
  if (statuses.every(s => s === 'success')) return 'success' as const
  if (statuses.some(s => s === 'running')) return 'info' as const
  if (statuses.some(s => s === 'failed')) return 'error' as const
  return 'default' as const
})

function agentIcon(type: string): string {
  const icons: Record<string, string> = { planner: '🎯', generator: '⚡', evaluator: '✅', worker: '🔧' }
  return icons[type] || '📋'
}

function statusIcon(status: NodeStatus): string {
  const icons: Record<string, string> = { pending: '⏳', success: '✅', failed: '❌', skipped: '⏭️', retrying: '🔄', pending_approval: '⏸️' }
  return icons[status] || '📋'
}

function truncate(s: string | null | undefined, len: number): string {
  if (!s) return ''
  return s.length > len ? s.slice(0, len) + '...' : s
}

function formatDuration(ms: number): string {
  if (ms < 1000) return ms + 'ms'
  if (ms < 60000) return (ms / 1000).toFixed(1) + 's'
  return (ms / 60000).toFixed(1) + 'min'
}

function measurePositions() {
  if (!containerRef.value) return
  const containerRect = containerRef.value.getBoundingClientRect()
  svgWidth.value = containerRect.width
  svgHeight.value = containerRect.height
}

function calcPath(edge: DAGEdge): string {
  const srcEl = nodeRefs[edge.from]
  const dstEl = nodeRefs[edge.to]
  const container = containerRef.value
  if (!srcEl || !dstEl || !container) return ''

  const cr = container.getBoundingClientRect()
  const sr = srcEl.getBoundingClientRect()
  const dr = dstEl.getBoundingClientRect()

  const sx = sr.left + sr.width / 2 - cr.left
  const sy = sr.top + sr.height - cr.top
  const dx = dr.left + dr.width / 2 - cr.left
  const dy = dr.top - cr.top
  const midY = (sy + dy) / 2

  return `M ${sx} ${sy} C ${sx} ${midY}, ${dx} ${midY}, ${dx} ${dy}`
}

function edgeColor(edge: DAGEdge): string {
  const status = getNode(edge.from).status
  if (status === 'success') return '#18a058'
  if (status === 'running') return '#2080f0'
  if (status === 'failed') return '#d03050'
  return '#d0d0d0'
}

watch(() => props.dag, () => {
  // Clear stale refs from previous DAG data
  Object.keys(nodeRefs).forEach(k => delete nodeRefs[k])
  nextTick(measurePositions)
})

onMounted(() => {
  nextTick(measurePositions)
  if (containerRef.value) {
    resizeObserver = new ResizeObserver(measurePositions)
    resizeObserver.observe(containerRef.value)
  }
})

onUnmounted(() => {
  resizeObserver?.disconnect()
})
</script>

<style scoped>
.dag-view { padding: 8px 0; }
.dag-container { position: relative; }
.dag-levels {
  display: flex;
  flex-direction: column;
  gap: 48px;
  padding: 24px 16px;
}
.dag-level {
  display: flex;
  justify-content: center;
  gap: 24px;
}
.dag-edges {
  position: absolute;
  top: 0;
  left: 0;
  pointer-events: none;
}

.dag-node {
  width: 200px;
  padding: 12px 14px;
  border-radius: 8px;
  border: 2px solid #ddd;
  background: #fafafa;
  transition: border-color 0.3s, background 0.3s, box-shadow 0.3s;
}
.dag-node--pending { border-color: #ddd; background: #fafafa; }
.dag-node--running { border-color: #2080f0; background: #e8f4fd; box-shadow: 0 0 8px rgba(32,128,240,0.15); }
.dag-node--success { border-color: #18a058; background: #e8f8e8; }
.dag-node--failed { border-color: #d03050; background: #fce8e8; }
.dag-node--skipped { border-color: #ccc; background: #f5f5f5; opacity: 0.7; }
.dag-node--retrying { border-color: #f0a020; background: #fff3e0; }
.dag-node--pending_approval { border-color: #f0a020; background: #fff8e8; }

.dag-node__header { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
.dag-node__icon { font-size: 14px; }
.dag-node__id { font-weight: 600; font-size: 13px; flex: 1; }
.dag-node__status { font-size: 12px; }
.dag-node__task { font-size: 12px; color: #666; line-height: 1.4; }
.dag-node__duration { font-size: 11px; color: #999; margin-top: 4px; }
.dag-node__error { font-size: 11px; color: #d03050; margin-top: 2px; }
</style>
