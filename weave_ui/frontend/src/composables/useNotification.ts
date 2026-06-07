import { ref } from 'vue'
import type { NotificationPrefs } from '../types'
import * as api from '../api'

const prefs = ref<NotificationPrefs>({ on_succeeded: true, on_failed: true, on_stuck: true, on_pending_approval: true })
const permission = ref<NotificationPermission>('default')

async function requestPermission() {
  if ('Notification' in window) {
    permission.value = await Notification.requestPermission()
  }
}

async function loadPrefs() {
  try { prefs.value = await api.getNotificationPrefs() } catch {}
}

async function savePrefs(p: NotificationPrefs) {
  prefs.value = await api.updateNotificationPrefs(p)
}

function notify(title: string, body: string, jobId?: string) {
  if (permission.value !== 'granted') return
  const n = new Notification(title, { body })
  if (jobId) n.onclick = () => { window.focus(); window.location.hash = '/jobs/' + jobId }
}

function onJobStatusChange(jobId: string, newStatus: string, requirement: string) {
  const summary = requirement.length > 60 ? requirement.slice(0, 60) + '...' : requirement
  if (newStatus === 'succeeded' && prefs.value.on_succeeded) notify('Task Succeeded', summary, jobId)
  else if (newStatus === 'failed' && prefs.value.on_failed) notify('Task Failed', summary, jobId)
  else if (newStatus === 'pending_approval' && prefs.value.on_pending_approval) notify('Approval Needed', summary, jobId)
}

export function useNotification() {
  return { prefs, permission, requestPermission, loadPrefs, savePrefs, onJobStatusChange }
}
