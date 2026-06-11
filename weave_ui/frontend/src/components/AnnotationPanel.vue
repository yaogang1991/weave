<template>
  <n-card title="Annotations" size="small">
    <n-space vertical>
      <n-dynamic-tags v-model:value="form.tags" />
      <n-input v-model:value="form.notes" type="textarea" :rows="3" placeholder="Add notes..." />
      <n-rate v-model:value="form.rating" />
      <n-button type="primary" size="small" @click="save">Save</n-button>
    </n-space>
  </n-card>
</template>
<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { NCard, NDynamicTags, NInput, NRate, NButton, NSpace, useMessage } from 'naive-ui'
import * as api from '../api'
const props = defineProps<{ jobId: string }>()
const msg = useMessage()
const form = ref({ tags: [] as string[], notes: '', rating: 0 })
onMounted(async () => { try { const a = await api.getAnnotations(props.jobId); form.value = { tags: a.tags || [], notes: a.notes || '', rating: a.rating || 0 } } catch {} })
async function save() {
  try { await api.updateAnnotations(props.jobId, form.value); msg.success('Saved') } catch (e: any) { msg.error(e.message) }
}
</script>
