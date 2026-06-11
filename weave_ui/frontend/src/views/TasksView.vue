<template>
  <div style="max-width: 720px">
    <n-h1>New Task</n-h1>
    <n-form ref="formRef" :model="form" :rules="rules" label-placement="top">
      <n-form-item label="Workspace" path="workspace">
        <n-select v-model:value="form.workspace" :options="wsOptions" filterable tag clearable placeholder="Select or type path" />
      </n-form-item>
      <n-form-item label="Template" path="template">
        <n-select v-model:value="form.template" :options="tplOptions" clearable placeholder="Optional: load a template" @update:value="onTemplate" />
      </n-form-item>
      <n-form-item label="Prompt" path="requirement">
        <n-input v-model:value="form.requirement" type="textarea" :rows="8" placeholder="Describe what you want to accomplish..." />
      </n-form-item>
      <n-space>
        <n-button type="primary" :loading="submitting" @click="handleSubmit">Submit</n-button>
      </n-space>
    </n-form>
  </div>
</template>
<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { NH1, NForm, NFormItem, NInput, NSelect, NButton, NSpace, useMessage } from 'naive-ui'
import type { FormInst, FormRules } from 'naive-ui'
import { useJobStore } from '../stores/job'
import { useWorkspaceStore } from '../stores/workspace'
import * as api from '../api'
import type { Template } from '../types'

const router = useRouter(), msg = useMessage(), jobStore = useJobStore(), wsStore = useWorkspaceStore()
const formRef = ref<FormInst | null>(null)
const submitting = ref(false)
const templates = ref<Template[]>([])

const form = ref({ workspace: null as string | null, template: null as string | null, requirement: '' })
const rules: FormRules = {
  workspace: { required: true, message: 'Workspace is required' },
  requirement: { required: true, message: 'Prompt is required' },
}
const wsOptions = computed(() => wsStore.workspaces.map(w => ({ label: w.label || w.path, value: w.path })))
const tplOptions = computed(() => templates.value.map(t => ({ label: t.name + ' - ' + t.description, value: t.name })))

function onTemplate(name: string | null) {
  if (!name) return
  const tpl = templates.value.find(t => t.name === name)
  if (tpl) form.value.requirement = tpl.description || ''
}

async function handleSubmit() {
  try { await formRef.value?.validate() } catch { return }
  if (!form.value.workspace || !form.value.requirement) return
  submitting.value = true
  try {
    await jobStore.submitJob({ requirement: form.value.requirement, workspace: form.value.workspace, template: form.value.template || undefined })
    msg.success('Task submitted!')
    router.push('/')
  } catch (e: any) { msg.error(e.message) }
  finally { submitting.value = false }
}

onMounted(async () => {
  wsStore.fetchWorkspaces()
  try { const res = await api.getTemplates(); templates.value = res.templates } catch {}
})
</script>
