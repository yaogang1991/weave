import type { Job, ApprovalTicket, Session, Template, Workspace, SubmitJobRequest, NotificationPrefs, SearchResult, TaskTemplate, Annotation } from "../types"

const BASE = "/api"

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, { headers: { "Content-Type": "application/json" }, ...options })
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText)
  return res.json()
}

// Jobs
export const getJobs = (status?: string) => request<Job[]>("/jobs" + (status ? "?status=" + status : ""))
export const getJob = (id: string) => request<Job & { runs: any[] }>("/jobs/" + id)
export const submitJob = (data: SubmitJobRequest) => request<Job>("/jobs", { method: "POST", body: JSON.stringify(data) })
export const cancelJob = (id: string) => request<Job>("/jobs/" + id + "/cancel", { method: "POST" })
export const retryJob = (id: string) => request<Job>("/jobs/" + id + "/retry", { method: "POST" })

// Sessions
export const getSessions = () => request<{ sessions: any[] }>("/sessions")
export const getSession = (id: string) => request<Session>("/sessions/" + id)

// Tickets
export const getTickets = (status?: string) => request<ApprovalTicket[]>("/tickets" + (status ? "?status=" + status : ""))
export const approveTicket = (id: string) => request<any>("/tickets/" + id + "/approve", { method: "POST" })
export const rejectTicket = (id: string, reason: string) => request<any>("/tickets/" + id + "/reject", { method: "POST", body: JSON.stringify({ reason }) })

// Templates
export const getTemplates = () => request<Template[]>("/templates")

// Workspaces
export const getWorkspaces = () => request<Workspace[]>("/workspaces")
export const addWorkspace = (path: string, label: string) => request<Workspace>("/workspaces", { method: "POST", body: JSON.stringify({ path, label }) })

// Summary
export const getJobSummary = (id: string) => request<{ title: string; content: string }>("/jobs/" + id + "/summary")

// Search (M8.3)
export const searchJobs = (params: { q?: string; status?: string; from?: string; to?: string }) => {
  const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v)).toString()
  return request<SearchResult>("/search?" + qs)
}

// Notification Preferences (M8.3)
export const getNotificationPrefs = () => request<NotificationPrefs>("/notification-preferences")
export const updateNotificationPrefs = (prefs: NotificationPrefs) => request<NotificationPrefs>("/notification-preferences", { method: "PUT", body: JSON.stringify(prefs) })

// Task Templates (M8.4)
export const getTaskTemplates = () => request<{ templates: TaskTemplate[] }>("/task-templates")
export const createTaskTemplate = (tpl: Omit<TaskTemplate, 'variables'> & { variables?: TaskTemplate['variables'] }) =>
  request<TaskTemplate>("/task-templates", { method: "POST", body: JSON.stringify(tpl) })
export const updateTaskTemplate = (name: string, tpl: Partial<TaskTemplate>) =>
  request<TaskTemplate>("/task-templates/" + encodeURIComponent(name), { method: "PUT", body: JSON.stringify(tpl) })
export const deleteTaskTemplate = (name: string) =>
  request<any>("/task-templates/" + encodeURIComponent(name), { method: "DELETE" })

// Annotations (M8.4)
export const getAnnotations = (jobId: string) => request<Annotation>("/jobs/" + jobId + "/annotations")
export const updateAnnotations = (jobId: string, data: Partial<Annotation>) =>
  request<Annotation>("/jobs/" + jobId + "/annotations", { method: "PUT", body: JSON.stringify(data) })
