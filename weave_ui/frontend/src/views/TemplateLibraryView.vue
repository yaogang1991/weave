<template>
  <div>
    <n-h1>Template Library</n-h1>
    <n-button type="primary" style="margin-bottom: 16px" @click="showForm = true">New Template</n-button>
    <n-grid :cols="2" :x-gap="12" :y-gap="12">
      <n-gi v-for="t in templates" :key="t.name">
        <n-card size="small" :title="t.name">
          <template #header-extra>
            <n-button text type="error" @click="doDelete(t.name)">Delete</n-button>
          </template>
          <n-text depth="2">{{ t.description }}</n-text>
          <template #footer><n-tag size="small">{{ t.category || 'general' }}</n-tag></template>
        </n-card>
      </n-gi>
    </n-grid>
    <n-modal v-model:show="showForm">
      <n-card title="New Template" style="width: 500px">
        <n-space vertical>
          <n-input v-model:value="newTpl.name" placeholder="Template name" />
          <n-input v-model:value="newTpl.description" placeholder="Description" />
          <n-input v-model:value="newTpl.category" placeholder="Category" />
          <n-input v-model:value="newTpl.prompt" type="textarea" :rows="4" placeholder="Prompt template" />
          <n-button type="primary" @click="doCreate">Create</n-button>
        </n-space>
      </n-card>
    </n-modal>
  </div>
</template>
<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { NH1, NGrid, NGi, NCard, NTag, NButton, NModal, NInput, NSpace, useMessage } from 'naive-ui'
import * as api from '../api'
const msg = useMessage()
const templates = ref<any[]>([])
const showForm = ref(false)
const newTpl = ref({ name: '', description: '', category: 'general', prompt: '' })
async function load() { try { const r = await api.getTaskTemplates(); templates.value = (r as any).templates || [] } catch {} }
onMounted(load)
async function doCreate() {
  try { await api.createTaskTemplate(newTpl.value); msg.success('Created'); showForm.value = false; await load() } catch (e: any) { msg.error(e.message) }
}
async function doDelete(name: string) {
  try { await api.deleteTaskTemplate(name); msg.info('Deleted'); await load() } catch (e: any) { msg.error(e.message) }
}
</script>
