export type HealthStatus = "HEALTHY" | "DEGRADED" | "UNAVAILABLE" | "UNKNOWN"

export interface OverallHealth {
  status: HealthStatus
  updatedAt: string
  componentCount: number
  unhealthyCount: number
  allowedActions: string[]
}

export interface ComponentStatus {
  name: string
  category: string
  status: HealthStatus
  critical: boolean
  source: string
  updatedAt: string
  message: string | null
  details: { key: string; value: string; unit: string | null }[]
  allowedActions: string[]
}

export interface WorkerStatus {
  workerId: string
  queue: string
  status: string
  currentJobId: string | null
  heartbeatAt: string | null
  processedJobs: number
  failedJobs: number
}

export interface QueueStatus {
  name: string
  status: HealthStatus
  depth: number
  activeWorkers: number
  oldestJobAt: string | null
  updatedAt: string
}

export interface RuntimeStatus {
  workers: WorkerStatus[]
  queues: QueueStatus[]
  allowedActions: string[]
}

export interface SchedulerStatus {
  status: HealthStatus
  scanIntervalSeconds: number
  lastScanAt: string | null
  databaseTime: string | null
  automaticSchedulingPaused: boolean
  pauseReason: string | null
  updatedAt: string
}

export interface ClockSource {
  source: string
  observedAt: string | null
  skewSeconds: number | null
  status: HealthStatus
}

export interface ClockStatus {
  status: HealthStatus
  applicationTime: string
  databaseTime: string | null
  maxSkewSeconds: number | null
  automaticSchedulingPaused: boolean
  sources: ClockSource[]
  updatedAt: string
}

export interface SchedulingStatus {
  scheduler: SchedulerStatus
  clock: ClockStatus
  allowedActions: string[]
}

export interface ScheduleOccurrence {
  occurrenceId: string
  occurrenceType: string
  definitionId: string
  scheduledTradeDate: string
  scheduledAt: string
  status: string
  jobId: string | null
  missedReason: string | null
  createdAt: string
  allowedActions: string[]
}

export interface OccurrencePage {
  items: ScheduleOccurrence[]
  page: number
  pageSize: number
  total: number
  allowedActions: string[]
}

export interface SystemStatusGateway {
  loadOverall(): Promise<OverallHealth>
  loadComponents(): Promise<ComponentStatus[]>
  loadRuntime(): Promise<RuntimeStatus>
  loadScheduling(): Promise<SchedulingStatus>
  loadOccurrences(): Promise<OccurrencePage>
}
