import type { components } from "@/shared/api/generated/schema"

export type SignalState = components["schemas"]["SignalStateView"]
export type SignalEvent = components["schemas"]["SignalEventView"]
export type SignalEvaluation = components["schemas"]["SignalEvaluationView"]
export type SignalZone = components["schemas"]["SignalZone"]
export type EvaluationReason = components["schemas"]["EvaluationReason"]
export type EvaluationResult = components["schemas"]["EvaluationResult"]

export interface PageResult<T> {
  items: T[]
  page: number
  pageSize: number
  total: number
}

export interface NotificationDeliverySummary {
  id: string
  channel: string
  status: string
  sentAt: string | null
  errorCode: string | null
}

export interface SignalEventItem extends SignalEvent {
  notificationStatus: string | null
  deliveries: NotificationDeliverySummary[]
}

export interface SignalEventPage extends PageResult<SignalEventItem> {
  warningCodes: string[]
}

export interface SignalsGateway {
  loadStates(page: number, pageSize: number): Promise<PageResult<SignalState>>
  loadEvents(page: number, pageSize: number): Promise<SignalEventPage>
  loadEvaluations(
    page: number,
    pageSize: number,
  ): Promise<PageResult<SignalEvaluation>>
}
