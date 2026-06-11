<template>
  <n-layout has-sider style="height: 100vh">
    <n-layout-sider bordered :width="220" :collapsed-width="64" collapse-mode="width" show-trigger>
      <div style="padding: 16px; font-size: 18px; font-weight: 600; text-align: center">Weave UI</div>
      <n-menu :options="menuOptions" :value="activeKey" @update:value="handleMenuSelect" />
    </n-layout-sider>
    <n-layout><n-layout-content content-style="padding: 24px"><router-view /></n-layout-content></n-layout>
  </n-layout>
</template>
<script setup lang="ts">
import { computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { NLayout, NLayoutSider, NLayoutContent, NMenu } from 'naive-ui'
import type { MenuOption } from 'naive-ui'
const router = useRouter(), route = useRoute()
const activeKey = computed(() => route.name as string)
const menuOptions: MenuOption[] = [
  { label: 'Dashboard', key: 'dashboard' },
  { label: 'Tasks', key: 'tasks' },
  { label: 'History', key: 'history' },
  { label: 'Settings', key: 'settings' },
]
function handleMenuSelect(key: string) { router.push({ name: key }) }
</script>
