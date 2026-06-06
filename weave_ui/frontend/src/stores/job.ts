import { ref, computed } from 'vue'
import { defineStore } from 'pinia'
import type { Job, SubmitJobRequest } from '../types'
import * as api from '../api'

export const useJobStore = defineStore('jobs', () => {
  const jobs = ref<Job[]>([])
  const currentJob = ref<any>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  const runningJobs = computed(() => jobs.value.filter(j => j.status === 'running' || j.status === 'leased'))
  const queuedJobs = computed(() => jobs.value.filter(j => j.status === 'queued'))
  const failedJobs = computed(() => jobs.value.filter(j => j.status === 'failed' || j.status === 'dead_letter'))
  const succeededJobs = computed(() => jobs.value.filter(j => j.status === 'succeeded'))

  async function fetchJobs() {
    loading.value = true
    error.value = null
    try {
      const res = await api.getJobs()
      jobs.value = (res as any).jobs ?? res
    } catch (e: any) {
      error.value = e.message
    } finally {
      loading.value = false
    }
  }

  async function fetchJob(id: string) {
    loading.value = true
    try {
      const res = await api.getJob(id)
      currentJob.value = res
    } catch (e: any) {
      error.value = e.message
    } finally {
      loading.value = false
    }
  }

  async function submitJob(data: SubmitJobRequest) {
    const job = await api.submitJob(data)
    await fetchJobs()
    return job
  }

  async function cancelJob(id: string) {
    await api.cancelJob(id)
    await fetchJobs()
  }

  async function retryJob(id: string) {
    await api.retryJob(id)
    await fetchJobs()
  }

  function updateJobStatus(jobId: string, status: string) {
    const job = jobs.value.find(j => j.id === jobId)
    if (job) job.status = status as any
  }

  return {
    jobs, currentJob, loading, error,
    runningJobs, queuedJobs, failedJobs, succeededJobs,
    fetchJobs, fetchJob, submitJob, cancelJob, retryJob, updateJobStatus,
  }
})
