import { z } from "zod"

import type {
  AlertActionRecord,
  AlertGateway,
  AlertItem,
  AlertOccurrence,
  AlertOperationResult,
  AlertPage,
} from "@/features/alerts/types"
import { ApiError, createApiClient, createClientIdempotencyKey } from "@/shared/api/client"
import type { paths } from "@/shared/api/generated/schema"

const severitySchema = z.enum(["INFO", "WARNING", "ERROR", "CRITICAL"])
const statusSchema = z.enum(["OPEN", "ACKNOWLEDGED", "RESOLVED"])
const allowedActionSchema = z.enum(["ACKNOWLEDGE", "RESOLVE", "RETRY"])
const historyActionSchema = z.enum([
  "OPENED",
  "UPDATED",
  "ESCALATED",
  "REOPENED",
  "ACKNOWLEDGED",
  "RESOLVED",
  "AUTO_RESOLVED",
  "RETRY_REQUESTED",
])

const alertSchema = z.object({
  id: z.string().min(1),
  aggregation_key: z.string().min(1),
  alert_type: z.string().min(1),
  object_type: z.string().min(1),
  object_id: z.string().min(1),
  severity: severitySchema,
  status: statusSchema,
  title: z.string().min(1),
  summary: z.string(),
  details: z.record(z.string(), z.unknown()),
  occurrence_count: z.number().int().positive(),
  first_seen_at: z.string().min(1),
  last_seen_at: z.string().min(1),
  acknowledged_at: z.string().nullable(),
  acknowledged_by_user_id: z.string().nullable(),
  resolved_at: z.string().nullable(),
  resolved_by_user_id: z.string().nullable(),
  resolution_reason: z.string().nullable(),
  version: z.number().int().positive(),
  created_at: z.string().min(1),
  updated_at: z.string().min(1),
  allowed_actions: z.array(allowedActionSchema).optional().default([]),
})

const occurrenceSchema = z.object({
  id: z.string().min(1),
  alert_id: z.string().min(1),
  source_event_id: z.string().min(1),
  severity: severitySchema,
  summary: z.string(),
  details: z.record(z.string(), z.unknown()),
  request_id: z.string().min(1),
  occurred_at: z.string().min(1),
})

const actionSchema = z.object({
  id: z.string().min(1),
  alert_id: z.string().min(1),
  action: historyActionSchema,
  reason: z.string().nullable(),
  actor_user_id: z.string().nullable(),
  request_id: z.string().min(1),
  job_id: z.string().nullable(),
  created_at: z.string().min(1),
})

function pageSchema<T extends z.ZodType>(itemSchema: T) {
  return z.object({
    items: z.array(itemSchema),
    total: z.number().int().nonnegative(),
    page: z.number().int().positive(),
    page_size: z.number().int().positive(),
  })
}

function parse<T>(schema: z.ZodType<T>, value: unknown, code: string): T {
  const result = schema.safeParse(value)
  if (!result.success) {
    throw new ApiError("告警接口返回的数据无法识别。", {
      code,
      cause: result.error,
    })
  }
  return result.data
}

function mapAlert(value: z.infer<typeof alertSchema>): AlertItem {
  return {
    id: value.id,
    aggregationKey: value.aggregation_key,
    alertType: value.alert_type,
    objectType: value.object_type,
    objectId: value.object_id,
    severity: value.severity,
    status: value.status,
    title: value.title,
    summary: value.summary,
    details: value.details,
    occurrenceCount: value.occurrence_count,
    firstSeenAt: value.first_seen_at,
    lastSeenAt: value.last_seen_at,
    acknowledgedAt: value.acknowledged_at,
    acknowledgedByUserId: value.acknowledged_by_user_id,
    resolvedAt: value.resolved_at,
    resolvedByUserId: value.resolved_by_user_id,
    resolutionReason: value.resolution_reason,
    version: value.version,
    createdAt: value.created_at,
    updatedAt: value.updated_at,
    allowedActions: value.allowed_actions,
  }
}

function mapPage<TSource, TTarget>(
  value: { items: TSource[]; total: number; page: number; page_size: number },
  mapItem: (item: TSource) => TTarget,
): AlertPage<TTarget> {
  return {
    items: value.items.map(mapItem),
    total: value.total,
    page: value.page,
    pageSize: value.page_size,
  }
}

export function createAlertGateway(baseUrl = ""): AlertGateway {
  const api = createApiClient<paths>({ baseUrl })

  return {
    async loadAlerts(filters) {
      const value = await api.request<unknown>(
        api.client.GET("/api/v1/alerts", {
          params: {
            query: {
              page: filters.page,
              page_size: filters.pageSize,
              status: filters.status,
              severity: filters.severity,
              alert_type: filters.alertType || undefined,
            },
          },
        }),
      )
      const page = parse(pageSchema(alertSchema), value, "ALERT_LIST_INVALID")
      return mapPage(page, mapAlert)
    },

    async loadAlert(alertId) {
      const value = await api.request<unknown>(
        api.client.GET("/api/v1/alerts/{alert_id}", {
          params: { path: { alert_id: alertId } },
        }),
      )
      return mapAlert(parse(alertSchema, value, "ALERT_DETAIL_INVALID"))
    },

    async loadOccurrences(alertId) {
      const value = await api.request<unknown>(
        api.client.GET("/api/v1/alerts/{alert_id}/occurrences", {
          params: {
            path: { alert_id: alertId },
            query: { page: 1, page_size: 50 },
          },
        }),
      )
      const page = parse(
        pageSchema(occurrenceSchema),
        value,
        "ALERT_OCCURRENCES_INVALID",
      )
      return mapPage(page, (item): AlertOccurrence => ({
        id: item.id,
        alertId: item.alert_id,
        sourceEventId: item.source_event_id,
        severity: item.severity,
        summary: item.summary,
        details: item.details,
        requestId: item.request_id,
        occurredAt: item.occurred_at,
      }))
    },

    async loadActions(alertId) {
      const value = await api.request<unknown>(
        api.client.GET("/api/v1/alerts/{alert_id}/actions", {
          params: {
            path: { alert_id: alertId },
            query: { page: 1, page_size: 50 },
          },
        }),
      )
      const page = parse(pageSchema(actionSchema), value, "ALERT_ACTIONS_INVALID")
      return mapPage(page, (item): AlertActionRecord => ({
        id: item.id,
        alertId: item.alert_id,
        action: item.action,
        reason: item.reason,
        actorUserId: item.actor_user_id,
        requestId: item.request_id,
        jobId: item.job_id,
        createdAt: item.created_at,
      }))
    },

    async runAction(input) {
      const parameters = {
        params: {
          path: { alert_id: input.alertId },
          header: { "Idempotency-Key": createClientIdempotencyKey() },
        },
        body: {
          expected_version: input.expectedVersion,
          reason: input.reason,
          confirm: true,
        },
      }
      if (input.action === "RETRY") {
        const value = await api.request<unknown>(
          api.client.POST("/api/v1/alerts/{alert_id}/retry", parameters),
        )
        const result = parse(
          z.object({ alert: alertSchema, job_id: z.string().min(1) }),
          value,
          "ALERT_RETRY_RESULT_INVALID",
        )
        return {
          alert: mapAlert(result.alert),
          jobId: result.job_id,
        } satisfies AlertOperationResult
      }
      const endpoint = input.action === "ACKNOWLEDGE"
        ? "/api/v1/alerts/{alert_id}/acknowledge" as const
        : "/api/v1/alerts/{alert_id}/resolve" as const
      const value = await api.request<unknown>(api.client.POST(endpoint, parameters))
      return {
        alert: mapAlert(parse(alertSchema, value, "ALERT_ACTION_RESULT_INVALID")),
        jobId: null,
      }
    },
  }
}

export const alertGateway = createAlertGateway()
