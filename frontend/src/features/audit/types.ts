export interface AuditEvent {
  id: string
  occurredAt: string
  actorUserId: string | null
  sessionId: string | null
  trustedIp: string | null
  actionCode: string
  objectType: string
  objectId: string
  result: string
  beforeSummary: Record<string, unknown> | null
  afterSummary: Record<string, unknown> | null
  reason: string | null
  requestId: string
  idempotencyKey: string
  riskLevel: string
}

export interface AuditFilters {
  page: number
  pageSize: number
  startAt?: string
  endAt?: string
  actorUserId?: string
  actionCode?: string
  objectType?: string
  objectId?: string
  result?: string
  riskLevel?: string
  requestId?: string
}

export interface AuditPage {
  items: AuditEvent[]
  pagination: {
    page: number
    pageSize: number
    total: number
  }
  allowedActions: string[]
}

export interface AuditGateway {
  loadEvents(filters: AuditFilters): Promise<AuditPage>
}
