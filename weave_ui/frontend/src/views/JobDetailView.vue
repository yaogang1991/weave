<template>
  <div v-if="jobStore.currentJob">
    <n-page-header @back="router.push('/')">
      <template #title>Job {{ jobId }}</template>
      <template #extra><StatusTag :status="jobStore.currentJob.job?.status" /></template>
    </n-page-header>
    <n-grid :cols="24" :x-gap="16" style="margin-top: 16px">
      <n-gi :span="16">
        <n-card title="Requirement" size="small" style="margin-bottom: 12px">
          <n-text>{{ jobStore.currentJob.job?.requirement }}</n-text>
        </n-card>
        <n-card title="Events" size="small">
          <EventTimeline :events="events" />
        </n-card>
      </n-gi>
      <n-gi :span="8">
        <n-card title="Actions" size="small" style="margin-bottom: 12px">
          <n-space vertical>
            <n-button v-if="canCancel" block type="warning" @click="doCancel">Cancel Job</n-button>
            <n-button v-if="canRetry" block type="info" @click="doRetry">Retry Job</n-button>
          </n-space>
        </n-card>
        <TicketList />
      </n-gi>
    </n-grid>
  </div>
  <n-spin v-else style="margin-top: 40px" />
</template>
<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { NPageHeader, NGrid, NGi, NCard, NText, NButton, NSpace, NSpin, useMessage } from 'naive-ui'
import { useJobStore } from '../stores/job'
import StatusTag from '../components/StatusTag.vue'
import EventTimeline from '../components/EventTimeline.vue'
import TicketList from '../components/TicketList.vue'
import * as api from '../api'
import type { SessionEvent } from '../types'

const router = useRouter(), route = useRoute(), msg = useMessage(), jobStore = useJobStore()
const events = ref<SessionEvent[]>([])
const jobId = computed(() => (route.params.id as string || '').slice(0, 16))
const status = computed(() => jobStore.currentJob?.job?.status)
const canCancel = computed(() => ['queued','running','leased','pending_approval'].includes(status.value))
const canRetry = computed(() => ['failed','dead_letter'].includes(status.value))

async function loadEvents() {
  const runs = jobStore.currentJob?.runs || []
  const sid = runs[runs.length - 1]?.session_id
  if (!sid) return
  try { const res = await api.getSession(sid); events.value = (res as any).events || [] } catch {}
}
async function doCancel() { try { await jobStore.cancelJob(route.params.id as string); msg.success('Canceled') } catch (e: any) { msg.error(e.message) } }
async function doRetry() { try { await jobStore.retryJob(route.params.id as string); msg.success('Retrying') } catch (e: any) { msg.error(e.message) } }

onMounted(async () => { await jobStore.fetchJob(route.params.id as string); await loadEvents() })
</script>
