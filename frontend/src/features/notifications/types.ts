export type NotificationAction =
  | "RETRY"
  | "CANCEL"
  | "UPDATE"
  | "TEST"
  | "PROBE"
  | "RESET_CIRCUIT"
  | "PREVIEW"
  | "ACTIVATE"

export type DeliveryChannel = "WECOM" | "EMAIL"
export type PolicyScope = "global" | "signals" | "system-alerts"
export type CircuitState = "CLOSED" | "OPEN" | "HALF_OPEN" | "DISABLED"

export interface PageResult<T> {
  items: T[]
  page: number
  pageSize: number
  total: number
}

export interface NotificationEvent {
  id: string
  eventType: string
  businessEventType: string
  businessObjectType: string
  businessObjectId: string
  severity: string
  status: string
  eligibilityStatus: string
  suppressionReason: string | null
  effectiveChannels: DeliveryChannel[]
  templateVersion: string
  createdAt: string
  allowedActions: NotificationAction[]
}

export interface NotificationDelivery {
  id: string
  eventId: string
  generation: number
  channel: DeliveryChannel
  targetFingerprint: string
  status: string
  attemptCount: number
  nextRetryAt: string | null
  sentAt: string | null
  errorCode: string | null
  createdAt: string
  updatedAt: string
  allowedActions: NotificationAction[]
  requiresDuplicateConfirmation: boolean
}

export interface NotificationAttempt {
  id: string
  deliveryId: string
  attemptNo: number
  phase: string
  durationMs: number | null
  outcome: string
  possiblyDelivered: boolean
  errorCode: string | null
  responseSummary: string | null
  startedAt: string
  finishedAt: string | null
}

export interface NotificationChannel {
  channel: DeliveryChannel
  enabled: boolean
  timeoutSeconds: number
  smtpHost: string | null
  smtpPort: number | null
  security: string | null
  username: string | null
  sender: string | null
  recipients: string[]
  version: number
  secretConfigured: boolean
  secretFingerprint: string | null
  circuitState: CircuitState
  circuitFailures: number
  circuitRetryAt: string | null
  allowedActions: NotificationAction[]
}

export interface NotificationPolicy {
  scope: PolicyScope
  enabled: boolean
  channels: DeliveryChannel[]
  warning: DeliveryChannel[]
  error: DeliveryChannel[]
  critical: DeliveryChannel[]
  recovered: DeliveryChannel[]
  dailyUnresolved: DeliveryChannel[]
  version: number
  allowedActions: NotificationAction[]
}

export interface NotificationTemplate {
  templateType: string
  version: string
  active: boolean
  createdAt: string
  allowedActions: NotificationAction[]
}

export interface TemplatePreview {
  subject: string | null
  text: string
  html: string | null
}

export interface NotificationGateway {
  loadEvents(): Promise<PageResult<NotificationEvent>>
  loadDeliveries(): Promise<PageResult<NotificationDelivery>>
  loadAttempts(deliveryId: string): Promise<PageResult<NotificationAttempt>>
  retryDelivery(input: {
    deliveryId: string
    reason: string
    confirmDuplicateRisk: boolean
  }): Promise<void>
  cancelDelivery(deliveryId: string, reason: string): Promise<void>
  loadChannels(): Promise<NotificationChannel[]>
  updateChannel(channel: NotificationChannel, reason: string): Promise<void>
  runChannelAction(input: {
    channel: DeliveryChannel
    action: "TEST" | "PROBE" | "RESET_CIRCUIT"
    reason: string
    message: string
  }): Promise<void>
  loadPolicy(scope: PolicyScope): Promise<NotificationPolicy>
  updatePolicy(policy: NotificationPolicy, reason: string): Promise<void>
  loadTemplates(): Promise<NotificationTemplate[]>
  previewTemplate(input: {
    templateType: string
    version: string
    variables: Record<string, unknown>
  }): Promise<TemplatePreview>
  activateTemplate(template: NotificationTemplate, reason: string): Promise<void>
}
