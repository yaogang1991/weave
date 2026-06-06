<template>
  <n-card title="Pending Approvals" v-if="tickets.length" size="small">
    <n-list bordered>
      <n-list-item v-for="t in tickets" :key="t.id">
        <n-thing>
          <template #header>{{ t.tool_name }}</template>
          <template #description>
            <n-text depth="3" style="font-size: 12px">{{ t.args_preview?.slice(0, 100) }}</n-text>
          </template>
        </n-thing>
        <template #action>
          <n-space>
            <n-button type="success" size="small" @click="handleApprove(t.id)">Approve</n-button>
            <n-button type="error" size="small" @click="handleReject(t.id)">Reject</n-button>
          </n-space>
        </template>
      </n-list-item>
    </n-list>
  </n-card>
</template>
<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { NCard, NList, NListItem, NThing, NButton, NSpace, NText, useMessage } from 'naive-ui'
import type { ApprovalTicket } from '../types'
import * as api from '../api'
const tickets = ref<ApprovalTicket[]>([])
const msg = useMessage()
async function load() { try { const res = await api.getTickets('pending'); tickets.value = (res as any).tickets ?? res } catch {} }
onMounted(load)
async function handleApprove(id: string) { try { await api.approveTicket(id); msg.success('Approved'); await load() } catch (e: any) { msg.error(e.message) } }
async function handleReject(id: string) { try { await api.rejectTicket(id, ''); msg.info('Rejected'); await load() } catch (e: any) { msg.error(e.message) } }
</script>
