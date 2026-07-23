import { z } from "zod"

import type {
  AuditEvent,
  AuditFilters,
  AuditGateway,
} from "@/features/audit/types"
import { ApiError, createApiClient } from "@/shared/api/client"
import type { paths } from "@/shared/api/generated/schema"

const eventSchema = z.object({
  id: z.string().uuid(),
  occurred_at: z.string(),
  actor_user_id: z.string().nullable(),
  session_id: z.string().nullable(),
  trusted_ip: z.string().nullable(),
  action_code: z.string(),
  object_type: z.string(),
  object_id: z.string(),
  result: z.string(),
  before_summary: z.record(z.string(), z.unknown()).nullable(),
  after_summary: z.record(z.string(), z.unknown()).nullable(),
  reason: z.string().nullable(),
  request_id: z.string(),
  idempotency_key: z.string(),
  risk_level: z.string(),
})

const pageSchema = z.object({
  items: z.array(eventSchema),
  pagination: z.object({
    page: z.number().int().positive(),
    page_size: z.number().int().positive(),
    total: z.number().int().nonnegative(),
  }),
  allowed_actions: z.array(z.string()).max(0),
})

function eventFrom(value: z.infer<typeof eventSchema>): AuditEvent {
  return {
    id: value.id,
    occurredAt: value.occurred_at,
    actorUserId: value.actor_user_id,
    sessionId: value.session_id,
    trustedIp: value.trusted_ip,
    actionCode: value.action_code,
    objectType: value.object_type,
    objectId: value.object_id,
    result: value.result,
    beforeSummary: value.before_summary,
    afterSummary: value.after_summary,
    reason: value.reason,
    requestId: value.request_id,
    idempotencyKey: value.idempotency_key,
    riskLevel: value.risk_level,
  }
}

function optional(value: string | undefined) {
  const trimmed = value?.trim()
  return trimmed || undefined
}

export function createAuditGateway(baseUrl = ""): AuditGateway {
  const api = createApiClient<paths>({ baseUrl })

  return {
    async loadEvents(filters: AuditFilters) {
      const response = await api.request<unknown>(
        api.client.GET("/api/v1/audit-events", {
          params: {
            query: {
              page: filters.page,
              page_size: filters.pageSize,
              start_at: optional(filters.startAt),
              end_at: optional(filters.endAt),
              actor_user_id: optional(filters.actorUserId),
              action_code: optional(filters.actionCode),
              object_type: optional(filters.objectType),
              object_id: optional(filters.objectId),
              result: optional(filters.result),
              risk_level: optional(filters.riskLevel),
              request_id: optional(filters.requestId),
            },
          },
        }),
      )
      const parsed = pageSchema.safeParse(response)
      if (!parsed.success) {
        throw new ApiError("审计接口返回的数据无法识别。", {
          code: "INVALID_AUDIT_EVENTS",
          cause: parsed.error,
        })
      }
      return {
        items: parsed.data.items.map(eventFrom),
        pagination: {
          page: parsed.data.pagination.page,
          pageSize: parsed.data.pagination.page_size,
          total: parsed.data.pagination.total,
        },
        allowedActions: parsed.data.allowed_actions,
      }
    },
  }
}

export const auditGateway = createAuditGateway()
