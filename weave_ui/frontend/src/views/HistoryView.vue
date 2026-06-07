<template>
  <div>
    <n-h1>History</n-h1>
    <SearchBar @search="doSearch" style="margin-bottom: 16px" />
    <n-spin :show="loading">
      <n-empty v-if="!jobs.length" description="No completed tasks yet" />
      <n-list v-else bordered>
        <n-list-item v-for="j in jobs" :key="j.id">
          <n-thing>
            <template #header>
              <router-link :to="'/jobs/' + j.id" style="text-decoration: none; color: inherit">
                <n-text code>{{ j.id.slice(0, 16) }}</n-text>
              </router-link>
            </template>
            <template #header-extra><StatusTag :status="j.status" /></template>
            <template #description>
              <n-text depth="3" style="font-size: 13px">{{ highlights[j.id] || j.requirement.slice(0, 120) }}</n-text>
            </template>
            <template #footer>
              <n-text depth="3" style="font-size: 12px">{{ formatTime(j.created_at) }}</n-text>
            </template>
          </n-thing>
        </n-list-item>
      </n-list>
    </n-spin>
  </div>
</template>
<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { NH1, NList, NListItem, NThing, NText, NEmpty, NSpin } from 'naive-ui'
import SearchBar from '../components/SearchBar.vue'
import StatusTag from '../components/StatusTag.vue'
import * as api from '../api'
import type { Job } from '../types'
const jobs = ref<Job[]>([])
const highlights = ref<Record<string, string>>({})
const loading = ref(false)

async function doSearch(params: { q: string; status: string | null }) {
  loading.value = true
  try {
    const res = await api.searchJobs({ q: params.q || undefined, status: params.status || undefined })
    jobs.value = (res as any).jobs || []
    highlights.value = (res as any).highlights || {}
  } catch { jobs.value = [] }
  finally { loading.value = false }
}

function formatTime(iso: string | null) { return iso ? new Date(iso).toLocaleString() : '' }

onMounted(() => doSearch({ q: '', status: null }))
</script>
