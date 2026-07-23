export type AlertSeverity = "INFO" | "WARNING" | "ERROR" | "CRITICAL"
export type AlertStatus = "OPEN" | "ACKNOWLEDGED" | "RESOLVED"
export type AlertAllowedAction = "ACKNOWLEDGE" | "RESOLVE" | "RETRY"
export type AlertHistoryAction =
  | "OPENED"
  | "UPDATED"
  | "ESCALATED"
  | "REOPENED"
  | "ACKNOWLEDGED"
  | "RESOLVED"
  | "AUTO_RESOLVED"
  | "RETRY_REQUESTED"

export interface AlertItem {
  id: string
  aggregationKey: string
  alertType: string
  objectType: string
  objectId: string
  severity: AlertSeverity
  status: AlertStatus
  title: string
  summary: string
  details: Record<string, unknown>
  occurrenceCount: number
  firstSeenAt: string
  lastSeenAt: string
  acknowledgedAt: string | null
  acknowledgedByUserId: string | null
  resolvedAt: string | null
  resolvedByUserId: string | null
  resolutionReason: string | null
  version: number
  createdAt: string
  updatedAt: string
  allowedActions: AlertAllowedAction[]
}

export interface AlertOccurrence {
  id: string
  alertId: string
  sourceEventId: string
  severity: AlertSeverity
  summary: string
  details: Record<string, unknown>
  requestId: string
  occurredAt: string
}

export interface AlertActionRecord {
  id: string
  alertId: string
  action: AlertHistoryAction
  reason: string | null
  actorUserId: string | null
  requestId: string
  jobId: string | null
  createdAt: string
}

export interface AlertPage<T> {
  items: T[]
  total: number
  page: number
  pageSize: number
}

export interface AlertFilters {
  page: number
  pageSize: number
  status?: AlertStatus
  severity?: AlertSeverity
  alertType?: string
}

export interface AlertOperationResult {
  alert: AlertItem
  jobId: string | null
}

export interface AlertGateway {
  loadAlerts(filters: AlertFilters): Promise<AlertPage<AlertItem>>
  loadAlert(alertId: string): Promise<AlertItem>
  loadOccurrences(alertId: string): Promise<AlertPage<AlertOccurrence>>
  loadActions(alertId: string): Promise<AlertPage<AlertActionRecord>>
  runAction(input: {
    alertId: string
    action: AlertAllowedAction
    expectedVersion: number
    reason: string
  }): Promise<AlertOperationResult>
}
