<template>
  <n-card title="Notification Preferences" size="small">
    <n-space vertical>
      <n-switch v-model:value="prefs.on_succeeded" @update:value="save"><template #header>Task Succeeded</template></n-switch>
      <n-switch v-model:value="prefs.on_failed" @update:value="save"><template #header>Task Failed</template></n-switch>
      <n-switch v-model:value="prefs.on_stuck" @update:value="save"><template #header>Task Stuck (15min)</template></n-switch>
      <n-switch v-model:value="prefs.on_pending_approval" @update:value="save"><template #header>Approval Needed</template></n-switch>
    </n-space>
    <n-button v-if="permission !== 'granted'" type="primary" style="margin-top: 12px" @click="requestPermission">Enable Browser Notifications</n-button>
  </n-card>
</template>
<script setup lang="ts">
import { onMounted } from 'vue'
import { NCard, NSwitch, NSpace, NButton } from 'naive-ui'
import { useNotificationStore } from '../stores/notification'
const { prefs, permission, requestPermission, loadPrefs, savePrefs } = useNotificationStore()
async function save() { await savePrefs(prefs) }
onMounted(() => { loadPrefs(); if (permission === 'default') requestPermission() })
</script>
