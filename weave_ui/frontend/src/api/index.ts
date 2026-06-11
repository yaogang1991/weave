import type { Job, ApprovalTicket, Session, Template, Workspace, SubmitJobRequest, NotificationPrefs, SearchResult, TaskTemplate, Annotation } from "../types"

const BASE = "/api"

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, { headers: { "Content-Type": "application/json" }, ...options })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || res.statusText)
  }
  // Handle 204 No Content
  if (res.status === 204) return undefined as T
  return res.json()
}

// ── Jobs ──────────────────────────────────────────────────────────────

interface JobListResponse { jobs: Job[]; count: number }
interface JobDetailResponse { job: Job; runs: Array<{ id: string; status: string; started_at: string | null; completed_at: string | null }> }

export const getJobs = (status?: string) => request<JobListResponse>("/jobs" + (status ? "?status=" + encodeURIComponent(status) : ""))
export const getJob = (id: string) => request<JobDetailResponse>("/jobs/" + encodeURIComponent(id))
export const submitJob = (data: SubmitJobRequest) => request<{ id: string; status: string; requirement: string; created_at: string | null }>("/jobs", { method: "POST", body: JSON.stringify(data) })
export const cancelJob = (id: string) => request<{ job_id: string; status: string; message: string }>("/jobs/" + encodeURIComponent(id) + "/cancel", { method: "POST" })
export const retryJob = (id: string) => request<{ job_id: string; status: string; message: string }>("/jobs/" + encodeURIComponent(id) + "/retry", { method: "POST" })

// ── Sessions ──────────────────────────────────────────────────────────

export const getSessions = () => request<{ sessions: any[] }>("/sessions")
export const getSession = (id: string) => request<Session>("/sessions/" + encodeURIComponent(id))

// ── Tickets ───────────────────────────────────────────────────────────

interface TicketListResponse { tickets: ApprovalTicket[]; count: number; stats: any }

export const getTickets = (status?: string) => request<TicketListResponse>("/tickets" + (status ? "?status=" + encodeURIComponent(status) : ""))
export const approveTicket = (id: string, reason?: string) => request<{ ticket_id: string; status: string; message: string }>("/tickets/" + encodeURIComponent(id) + "/approve", { method: "POST", body: reason ? JSON.stringify({ reason }) : undefined })
export const rejectTicket = (id: string, reason: string) => request<{ ticket_id: string; status: string; message: string }>("/tickets/" + encodeURIComponent(id) + "/reject", { method: "POST", body: JSON.stringify({ reason }) })

// ── Templates ─────────────────────────────────────────────────────────

export const getTemplates = () => request<{ templates: Template[]; count: number }>("/templates")

// ── Workspaces ────────────────────────────────────────────────────────

export const getWorkspaces = () => request<{ workspaces: Workspace[] }>("/workspaces")
export const addWorkspace = (path: string, label: string) => request<Workspace>("/workspaces", { method: "POST", body: JSON.stringify({ path, label }) })

// ── Summary ───────────────────────────────────────────────────────────

export const getJobSummary = (id: string) => request<{ title: string; content: string }>("/jobs/" + encodeURIComponent(id) + "/summary")

// ── Search (M8.3) ─────────────────────────────────────────────────────

export const searchJobs = (params: { q?: string; status?: string; from?: string; to?: string; limit?: number; offset?: number }) => {
  const qs = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v != null).map(([k, v]) => [k, String(v)])
  ).toString()
  return request<SearchResult>("/search?" + qs)
}

// ── Notification Preferences (M8.3) ──────────────────────────────────

export const getNotificationPrefs = () => request<NotificationPrefs>("/notification-preferences")
export const updateNotificationPrefs = (prefs: Partial<NotificationPrefs>) => request<NotificationPrefs>("/notification-preferences", { method: "PUT", body: JSON.stringify(prefs) })

// ── Task Templates (M8.4) ────────────────────────────────────────────

export const getTaskTemplates = () => request<{ templates: TaskTemplate[]; count: number }>("/task-templates")
export const createTaskTemplate = (tpl: { name: string; description?: string; category?: string; prompt?: string }) =>
  request<TaskTemplate>("/task-templates", { method: "POST", body: JSON.stringify(tpl) })
export const updateTaskTemplate = (name: string, tpl: Partial<TaskTemplate>) =>
  request<TaskTemplate>("/task-templates/" + encodeURIComponent(name), { method: "PUT", body: JSON.stringify(tpl) })
export const deleteTaskTemplate = (name: string) =>
  request<{ deleted: string }>("/task-templates/" + encodeURIComponent(name), { method: "DELETE" })

// ── Annotations (M8.4) ───────────────────────────────────────────────

export const getAnnotations = (jobId: string) => request<Annotation>("/jobs/" + encodeURIComponent(jobId) + "/annotations")
export const updateAnnotations = (jobId: string, data: Partial<Pick<Annotation, "tags" | "notes" | "rating">>) =>
  request<Annotation>("/jobs/" + encodeURIComponent(jobId) + "/annotations", { method: "PUT", body: JSON.stringify(data) })
