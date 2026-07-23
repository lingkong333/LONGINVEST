import { z } from "zod"

import type {
  NotificationDeliverySummary,
  PageResult,
  SignalEvaluation,
  SignalEvent,
  SignalEventPage,
  SignalState,
  SignalsGateway,
} from "@/features/signals/types"
import { ApiError, createApiClient } from "@/shared/api/client"
import type { paths } from "@/shared/api/generated/schema"

const paginationSchema = z.object({
  page: z.number().int().positive(),
  page_size: z.number().int().positive(),
  total: z.number().int().nonnegative(),
})

const targetValuesSchema = z.object({
  low_strong: z.string(),
  low_watch: z.string(),
  high_watch: z.string(),
  high_strong: z.string(),
})

const zoneSchema = z.enum([
  "UNKNOWN",
  "STRONG_LOW",
  "LOW",
  "NORMAL",
  "HIGH",
  "STRONG_HIGH",
])

const reasonSchema = z.enum([
  "SCHEDULED_QUOTE",
  "MANUAL_CHECK",
  "TARGET_ACTIVATED",
  "POSITION_BECAME_HOLDING",
  "DATA_CORRECTION",
  "STATE_RESET",
  "RECOVERY_REEVALUATION",
])

const stateSchema = z.object({
  subscription_id: z.string(),
  zone: zoneSchema,
  version: z.number().int().positive(),
  last_price: z.string().nullable().optional(),
  last_price_at: z.string().nullable().optional(),
  last_subscription_version: z.number().int().nullable().optional(),
  last_price_version: z.number().int().nullable().optional(),
  last_quote_cycle_id: z.string().nullable().optional(),
  last_quote_scheduled_at: z.string().nullable().optional(),
  last_quote_item_id: z.string().nullable().optional(),
  last_target_revision_id: z.string().nullable().optional(),
  last_target_version: z.number().int().nullable().optional(),
  last_position_version: z.number().int().nullable().optional(),
})

const eventSchema = z.object({
  id: z.string(),
  subscription_id: z.string(),
  evaluation_id: z.string(),
  before_zone: zoneSchema,
  after_zone: zoneSchema,
  reason: reasonSchema,
  price: z.string(),
  price_at: z.string(),
  targets: targetValuesSchema,
  target_revision_id: z.string(),
  target_version: z.number().int(),
  target_date: z.string(),
  position_status: z.enum(["HOLDING", "NOT_HOLDING"]),
  position_version: z.number().int(),
  quote_cycle_id: z.string().nullable().optional(),
  quote_scheduled_at: z.string().nullable().optional(),
  quote_item_id: z.string().nullable().optional(),
  used_stale_target: z.boolean(),
  state_version: z.number().int(),
  notification_class: z.enum(["LOW", "LOW_CLEARED", "HIGH", "HIGH_CLEARED"]),
  notification_eligible: z.boolean(),
  suppression_reason: z.string().nullable().optional(),
  created_at: z.string(),
})

const evaluationSchema = z.object({
  id: z.string(),
  subscription_id: z.string(),
  reason: reasonSchema,
  result: z.enum(["APPLIED", "UNCHANGED", "SKIPPED", "SUPERSEDED"]),
  before_zone: zoneSchema,
  after_zone: zoneSchema,
  subscription_version: z.number().int().nullable().optional(),
  target_revision_id: z.string().nullable().optional(),
  target_version: z.number().int().nullable().optional(),
  target_date: z.string().nullable().optional(),
  targets: targetValuesSchema.nullable().optional(),
  position_status: z.enum(["HOLDING", "NOT_HOLDING"]).nullable().optional(),
  position_version: z.number().int().nullable().optional(),
  price: z.string().nullable().optional(),
  price_at: z.string().nullable().optional(),
  price_version: z.number().int().nullable().optional(),
  quote_cycle_id: z.string().nullable().optional(),
  quote_scheduled_at: z.string().nullable().optional(),
  quote_item_id: z.string().nullable().optional(),
  hysteresis_applied: z.boolean(),
  used_stale_target: z.boolean(),
  skip_code: z.string().nullable().optional(),
  content_hash: z.string(),
  created_at: z.string(),
})

function pageSchema<T extends z.ZodType>(itemSchema: T) {
  return z.object({
    items: z.array(itemSchema),
    pagination: paginationSchema,
  })
}

const notificationEventPageSchema = z.object({
  items: z.array(z.object({
    id: z.string(),
    business_event_id: z.string(),
    business_event_type: z.string(),
    status: z.string(),
  })),
  page: z.number().int().positive(),
  page_size: z.number().int().positive(),
  total: z.number().int().nonnegative(),
})

const notificationDeliveryPageSchema = z.object({
  items: z.array(z.object({
    id: z.string(),
    event_id: z.string(),
    channel: z.string(),
    status: z.string(),
    sent_at: z.string().nullable(),
    error_code: z.string().nullable(),
  })),
  page: z.number().int().positive(),
  page_size: z.number().int().positive(),
  total: z.number().int().nonnegative(),
})

function parsePage<T>(
  value: unknown,
  schema: z.ZodType<{ items: T[]; pagination: z.infer<typeof paginationSchema> }>,
  code: string,
): PageResult<T> {
  const parsed = schema.safeParse(value)
  if (!parsed.success) {
    throw new ApiError("信号接口返回了无法识别的数据。", {
      code,
      cause: parsed.error,
    })
  }
  return {
    items: parsed.data.items,
    page: parsed.data.pagination.page,
    pageSize: parsed.data.pagination.page_size,
    total: parsed.data.pagination.total,
  }
}

export function createSignalsGateway(baseUrl = ""): SignalsGateway {
  const api = createApiClient<paths>({ baseUrl })

  return {
    async loadStates(page, pageSize) {
      const value = await api.request<unknown>(
        api.client.GET("/api/v1/signals/states", {
          params: { query: { page, page_size: pageSize } },
        }),
      )
      return parsePage<SignalState>(
        value,
        pageSchema(stateSchema),
        "INVALID_SIGNAL_STATE_RESPONSE",
      )
    },

    async loadEvents(page, pageSize) {
      const [eventsResult, notificationEventsResult, deliveriesResult] =
        await Promise.allSettled([
          api.request<unknown>(
            api.client.GET("/api/v1/signal-events", {
              params: { query: { page, page_size: pageSize } },
            }),
          ),
          api.request<unknown>(
            api.client.GET("/api/v1/notifications/events", {
              params: { query: { page: 1, page_size: 200 } },
            }),
          ),
          api.request<unknown>(
            api.client.GET("/api/v1/notifications/deliveries", {
              params: { query: { page: 1, page_size: 200 } },
            }),
          ),
        ])

      if (eventsResult.status === "rejected") {
        throw eventsResult.reason
      }
      const events = parsePage<SignalEvent>(
        eventsResult.value,
        pageSchema(eventSchema),
        "INVALID_SIGNAL_EVENT_RESPONSE",
      )
      const warningCodes: string[] = []
      const notificationBySignalEvent = new Map<
        string,
        { id: string; status: string }
      >()
      const deliveriesByEvent = new Map<string, NotificationDeliverySummary[]>()

      if (notificationEventsResult.status === "fulfilled") {
        const parsed = notificationEventPageSchema.safeParse(
          notificationEventsResult.value,
        )
        if (parsed.success) {
          for (const item of parsed.data.items) {
            if (item.business_event_type === "signal.transitioned") {
              notificationBySignalEvent.set(item.business_event_id, item)
            }
          }
        } else {
          warningCodes.push("INVALID_NOTIFICATION_EVENT_RESPONSE")
        }
      } else {
        warningCodes.push("NOTIFICATION_EVENTS_UNAVAILABLE")
      }

      if (deliveriesResult.status === "fulfilled") {
        const parsed = notificationDeliveryPageSchema.safeParse(
          deliveriesResult.value,
        )
        if (parsed.success) {
          for (const item of parsed.data.items) {
            const entries = deliveriesByEvent.get(item.event_id) ?? []
            entries.push({
              id: item.id,
              channel: item.channel,
              status: item.status,
              sentAt: item.sent_at,
              errorCode: item.error_code,
            })
            deliveriesByEvent.set(item.event_id, entries)
          }
        } else {
          warningCodes.push("INVALID_NOTIFICATION_DELIVERY_RESPONSE")
        }
      } else {
        warningCodes.push("NOTIFICATION_DELIVERIES_UNAVAILABLE")
      }

      return {
        ...events,
        warningCodes,
        items: events.items.map((event) => {
          const notification = notificationBySignalEvent.get(event.id)
          return {
            ...event,
            notificationStatus: notification?.status ?? null,
            deliveries: notification
              ? deliveriesByEvent.get(notification.id) ?? []
              : [],
          }
        }),
      } satisfies SignalEventPage
    },

    async loadEvaluations(page, pageSize) {
      const value = await api.request<unknown>(
        api.client.GET("/api/v1/signal-evaluations", {
          params: { query: { page, page_size: pageSize } },
        }),
      )
      return parsePage<SignalEvaluation>(
        value,
        pageSchema(evaluationSchema),
        "INVALID_SIGNAL_EVALUATION_RESPONSE",
      )
    },
  }
}

export const signalsGateway = createSignalsGateway()
