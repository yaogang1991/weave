export type JobStatus = "queued" | "leased" | "running" | "pending_approval" | "succeeded" | "failed" | "canceled" | "dead_letter"
export type RunStatus = "running" | "succeeded" | "failed" | "canceled"
export type TicketStatus = "pending" | "approved" | "consumed" | "rejected" | "expired"

export interface Job {
  id: string
  requirement: string
  status: JobStatus
  project_path: string | null
  attempt: number
  last_error: string
  error_category: string
  created_at: string
  updated_at: string
  lease_owner: string | null
  metadata: Record<string, any>
}

export interface Run {
  id: string
  job_id: string
  session_id: string
  status: RunStatus
  dag_result: Record<string, any>
  started_at: string
  completed_at: string | null
  created_at: string
}

export interface ApprovalTicket {
  id: string
  job_id: string
  run_id: string | null
  node_id: string | null
  tool_name: string
  args_preview: string
  risk_level: string
  status: TicketStatus
  requested_at: string
  decided_at: string | null
  reason: string
}

export interface SessionEvent {
  timestamp: string
  event_type: string
  agent_type?: string
  payload: Record<string, any>
}

export interface Session {
  id: string
  events: SessionEvent[]
  dag: Record<string, any> | null
}

export interface Template {
  name: string
  description: string
  category: string
  variables: Record<string, string>
}

export interface Workspace {
  path: string
  label: string
}

export interface SubmitJobRequest {
  requirement: string
  workspace: string
  template?: string
  priority?: number
}

export interface NotificationPrefs {
  on_succeeded: boolean
  on_failed: boolean
  on_stuck: boolean
  on_pending_approval: boolean
}

export interface SearchResult {
  jobs: Job[]
  highlights: Record<string, string>
  count: number
}

export interface TaskTemplate {
  name: string
  description: string
  category: string
  prompt: string
  variables: { name: string; description: string; required: boolean }[]
}

export interface Annotation {
  job_id: string
  tags: string[]
  notes: string
  rating: number
  updated_at: string
}
