<template>
  <n-card title="Notification Preferences" size="small">
    <n-space vertical>
      <n-space v-for="item in items" :key="item.key" align="center">
        <n-switch v-model:value="prefs[item.key]" @update:value="save" />
        <n-text>{{ item.label }}</n-text>
      </n-space>
    </n-space>
    <n-button v-if="permission !== 'granted'" type="primary" style="margin-top: 12px" @click="requestPermission">Enable Browser Notifications</n-button>
  </n-card>
</template>
<script setup lang="ts">
import { onMounted } from 'vue'
import { NCard, NSwitch, NSpace, NButton, NText } from 'naive-ui'
import { useNotificationStore } from '../stores/notification'
const { prefs, permission, requestPermission, loadPrefs, savePrefs } = useNotificationStore()
const items = [
  { key: 'on_succeeded' as const, label: 'Task Succeeded' },
  { key: 'on_failed' as const, label: 'Task Failed' },
  { key: 'on_stuck' as const, label: 'Task Stuck (15min)' },
  { key: 'on_pending_approval' as const, label: 'Approval Needed' },
]
async function save() { await savePrefs(prefs) }
onMounted(() => { loadPrefs(); if (permission === 'default') requestPermission() })
</script>
