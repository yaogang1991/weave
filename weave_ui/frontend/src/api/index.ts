import type { Job, ApprovalTicket, Session, Template, Workspace, SubmitJobRequest } from "../types"

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

// Search (M8.3 placeholder)
export const searchJobs = (q: string) => request<Job[]>("/search?q=" + encodeURIComponent(q))
