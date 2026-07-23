export const jobStatuses = [
  "PENDING_DISPATCH",
  "QUEUED",
  "RUNNING",
  "WAITING_RETRY",
  "PAUSING",
  "PAUSED",
  "CANCEL_REQUESTED",
  "SUCCEEDED",
  "PARTIAL",
  "FAILED",
  "TIMED_OUT",
  "LOST",
  "CANCELED",
  "BLOCKED",
  "REJECTED",
] as const

export type JobStatus = (typeof jobStatuses)[number]

export const jobItemStatuses = [
  "PENDING",
  "FETCHING",
  "VALIDATING",
  "RUNNING",
  "SAVING",
  "SUCCEEDED",
  "FAILED",
  "SKIPPED",
  "CANCELED",
] as const

export type JobItemStatus = (typeof jobItemStatuses)[number]

export type JobAction =
  | "cancel"
  | "pause"
  | "resume"
  | "retry"
  | "retry-failed-items"

export interface Pagination {
  page: number
  pageSize: number
  total: number
}

export interface JobSummary {
  id: string
  jobType: string
  businessObjectType: string | null
  businessObjectId: string | null
  queue: string
  priority: number
  status: JobStatus
  progress: Record<string, unknown> | null
  resultSummary: Record<string, unknown> | null
  currentRunId: string | null
  version: number
  createdAt: string
  updatedAt: string
  terminalAt: string | null
}

export interface JobDetail extends JobSummary {
  configSnapshot: Record<string, unknown>
  requestId: string
  createdByUserId: string | null
  softTimeoutSeconds: number
  hardTimeoutSeconds: number
}

export interface JobRun {
  id: string
  jobId: string
  attemptNo: number
  workerId: string | null
  status: string
  claimedAt: string | null
  startedAt: string | null
  endedAt: string | null
  heartbeatAt: string | null
  exitType: string | null
  errorCode: string | null
  errorSummary: string | null
  metrics: Record<string, unknown> | null
}

export interface JobItem {
  id: string
  jobId: string
  itemKey: string
  status: JobItemStatus
  attemptCount: number
  resultRef: string | null
  errorCode: string | null
  createdAt: string
  startedAt: string | null
  endedAt: string | null
  updatedAt: string
}

export interface JobFilters {
  page: number
  pageSize: number
  status?: JobStatus
  jobType?: string
  queue?: string
  createdFrom?: string
  createdTo?: string
}

export interface JobDetails {
  job: JobDetail
  runs: JobRun[]
  items: JobItem[]
  itemPagination: Pagination
  allowedActions: JobAction[]
}

export interface JobGateway {
  loadJobs(filters: JobFilters): Promise<{
    items: JobSummary[]
    pagination: Pagination
  }>
  loadDetails(jobId: string): Promise<JobDetails>
  runAction(input: {
    jobId: string
    action: JobAction
    reason: string
    expectedVersion: number
  }): Promise<void>
}
