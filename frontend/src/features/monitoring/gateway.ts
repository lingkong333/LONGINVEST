import { z } from "zod"

import type {
  MonitoringAction,
  MonitoringGateway,
  MonitoringOverview,
  MonitoringOverviewItem,
  MonitoringExecutionOverview,
  MonitorSchedule,
} from "@/features/monitoring/types"
import { ApiError, createApiClient, createClientIdempotencyKey } from "@/shared/api/client"
import type { paths } from "@/shared/api/generated/schema"

const monitoringActionSchema = z.enum([
  "ENABLE",
  "DISABLE",
  "ARCHIVE",
  "RESTORE",
  "CHECK_NOW",
  "DIAGNOSE",
])

const subscriptionSchema = z.object({
  id: z.string().min(1),
  symbol: z.string().min(1),
  status: z.string().min(1),
  version: z.number().int().positive(),
  current_revision_id: z.string().nullable(),
  allowed_actions: z.array(monitoringActionSchema),
})

const subscriptionListSchema = z.object({
  items: z.array(subscriptionSchema),
})

const revisionSchema = z.object({
  id: z.string().min(1),
  schedule_id: z.string().nullable(),
  target_mode: z.string().min(1),
  strategy_version_id: z.string().nullable(),
})

const subscriptionDetailSchema = z.object({
  subscription: subscriptionSchema,
  revisions: z.array(revisionSchema),
})

const watchlistListSchema = z.object({
  items: z.array(z.object({
    name: z.string().min(1),
    items: z.array(z.object({ symbol: z.string().min(1) })),
  })),
})

const positionListSchema = z.object({
  items: z.array(z.object({
    symbol: z.string().min(1),
    status: z.enum(["HOLDING", "NOT_HOLDING"]),
  })),
})

const scheduleListSchema = z.object({
  items: z.array(z.object({
    id: z.string().min(1),
    name: z.string().min(1),
    version: z.number().int().positive().default(1),
  })),
})

const scheduleDetailSchema = z.object({
  schedule: z.object({
    id: z.string().min(1),
    name: z.string().min(1),
    version: z.number().int().positive(),
  }),
  revision: z.object({ times: z.array(z.string().regex(/^\d{2}:\d{2}$/)) }),
})

const targetListSchema = z.object({
  items: z.array(z.object({
    subscription_id: z.string().min(1),
    status: z.string().min(1),
  })),
})

const signalListSchema = z.object({
  items: z.array(z.object({
    subscription_id: z.string().min(1),
    zone: z.string().min(1),
    last_price: z.string().nullable().optional(),
    last_price_at: z.string().nullable().optional(),
  })),
})

const securitySchema = z.object({
  symbol: z.string().min(1),
  name: z.string().min(1),
})

const occurrencePageSchema = z.object({
  items: z.array(z.object({
    occurrence_id: z.string().min(1),
    scheduled_at: z.string().min(1),
    status: z.string().min(1),
  })),
})

const quoteCyclePageSchema = z.object({
  items: z.array(z.object({
    schedule_occurrence_id: z.string().nullable(),
    status: z.string().min(1),
    expected_count: z.number().int().nonnegative(),
    valid_count: z.number().int().nonnegative(),
    failed_count: z.number().int().nonnegative(),
    started_at: z.string().nullable(),
    finalized_at: z.string().nullable(),
  })),
})

function shanghaiDate(value = new Date()) {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(value)
}

function shanghaiTime(value: string) {
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Shanghai",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value))
}

function cycleStatus(value: string) {
  if (value === "READY") return "SUCCEEDED" as const
  if (value === "PARTIAL") return "PARTIAL" as const
  if (["FAILED", "MISSED", "CANCELED"].includes(value)) return "FAILED" as const
  return "RUNNING" as const
}

function failureCode(error: unknown, fallback: string) {
  return error instanceof ApiError ? error.code : fallback
}

function parse<T>(
  schema: z.ZodType<T>,
  value: unknown,
  code: string,
): T {
  const parsed = schema.safeParse(value)
  if (!parsed.success) {
    throw new ApiError("监控列表响应结构无效。", {
      code,
      cause: parsed.error,
    })
  }
  return parsed.data
}

function valueOrWarning<T>(
  result: PromiseSettledResult<unknown>,
  schema: z.ZodType<T>,
  warningCode: string,
  fallback: T,
  warnings: string[],
) {
  if (result.status === "rejected") {
    warnings.push(failureCode(result.reason, warningCode))
    return fallback
  }
  try {
    return parse(schema, result.value, warningCode)
  } catch (error) {
    warnings.push(failureCode(error, warningCode))
    return fallback
  }
}

export function createMonitoringGateway(baseUrl = ""): MonitoringGateway {
  const api = createApiClient<paths>({ baseUrl })

  return {
    async loadTodaySnapshotStatus() {
      const today = shanghaiDate()
      const [schedules, occurrenceValue, cycleValue] = await Promise.all([
        this.loadSchedules(),
        api.request<unknown>(api.client.GET("/api/v1/schedule-occurrences", {
          params: { query: { page: 1, page_size: 200, occurrence_type: "REALTIME_QUOTE", from_date: today, through_date: today } },
        })),
        api.request<unknown>(api.client.GET("/api/v1/quote-cycles", {
          params: { query: { page: 1, page_size: 200 } },
        })),
      ])
      const occurrences = parse(occurrencePageSchema, occurrenceValue, "INVALID_MONITOR_OCCURRENCES_RESPONSE").items
      const cycles = parse(quoteCyclePageSchema, cycleValue, "INVALID_QUOTE_CYCLES_RESPONSE").items
      const cycleByOccurrence = new Map(cycles.filter((cycle) => cycle.schedule_occurrence_id).map((cycle) => [cycle.schedule_occurrence_id, cycle]))
      const occurrenceByTime = new Map(occurrences.map((occurrence) => [shanghaiTime(occurrence.scheduled_at), occurrence]))
      const nowTime = shanghaiTime(new Date().toISOString())
      const times = [...new Set(schedules.flatMap((schedule) => schedule.times))].sort()
      const items = times.map((scheduledTime) => {
        const occurrence = occurrenceByTime.get(scheduledTime)
        const cycle = occurrence ? cycleByOccurrence.get(occurrence.occurrence_id) : undefined
        if (cycle) {
          const durationSeconds = cycle.started_at && cycle.finalized_at
            ? Math.max(0, Math.round((new Date(cycle.finalized_at).getTime() - new Date(cycle.started_at).getTime()) / 1000))
            : null
          return { scheduledTime, status: cycleStatus(cycle.status), expectedCount: cycle.expected_count, fetchedCount: cycle.valid_count, failedCount: cycle.failed_count, startedAt: cycle.started_at, completedAt: cycle.finalized_at, durationSeconds }
        }
        const failed = occurrence && ["MISSED", "FAILED"].includes(occurrence.status)
        const running = occurrence && ["CLAIMED", "DISPATCHED"].includes(occurrence.status)
        return { scheduledTime, status: failed ? "FAILED" as const : running ? "RUNNING" as const : scheduledTime > nowTime ? "PENDING" as const : "NOT_EXECUTED" as const, expectedCount: 0, fetchedCount: 0, failedCount: 0, startedAt: null, completedAt: null, durationSeconds: null }
      })
      const hasAttention = items.some((item) => ["NOT_EXECUTED", "PARTIAL", "FAILED"].includes(item.status))
      const hasRunning = items.some((item) => item.status === "RUNNING")
      const hasSuccess = items.some((item) => item.status === "SUCCEEDED")
      return {
        overallStatus: times.length === 0 ? "NOT_CONFIGURED" : hasAttention ? "ATTENTION" : hasRunning ? "RUNNING" : hasSuccess ? "NORMAL" : "PENDING",
        plannedCount: items.length,
        executedCount: items.filter((item) => ["SUCCEEDED", "PARTIAL", "FAILED"].includes(item.status)).length,
        fetchedCount: items.reduce((total, item) => total + item.fetchedCount, 0),
        items,
      } satisfies MonitoringExecutionOverview
    },
    async loadSchedules() {
      const schedules = parse(
        scheduleListSchema,
        await api.request<unknown>(api.client.GET("/api/v1/monitor-schedules")),
        "INVALID_MONITOR_SCHEDULES_RESPONSE",
      )
      const details = await Promise.all(schedules.items.map(async (schedule) => (
        parse(
          scheduleDetailSchema,
          await api.request<unknown>(api.client.GET("/api/v1/monitor-schedules/{schedule_id}", {
            params: { path: { schedule_id: schedule.id } },
          })),
          "INVALID_MONITOR_SCHEDULE_RESPONSE",
        )
      )))
      return details.map(({ schedule, revision }) => ({
        id: schedule.id,
        name: schedule.name,
        version: schedule.version,
        times: revision.times,
      })) satisfies MonitorSchedule[]
    },
    async saveSchedule(input) {
      const body = {
        name: input.name,
        times: input.times,
        reason: input.reason,
        confirm: true as const,
      }
      if (input.id) {
        await api.request(api.client.PATCH("/api/v1/monitor-schedules/{schedule_id}", {
          params: {
            path: { schedule_id: input.id },
            header: { "Idempotency-Key": createClientIdempotencyKey() },
          },
          body: { ...body, expected_version: input.version ?? 1 },
        }))
        return
      }
      await api.request(api.client.POST("/api/v1/monitor-schedules", {
        params: { header: { "Idempotency-Key": createClientIdempotencyKey() } },
        body,
      }))
    },
    async loadOverview() {
      const subscriptions = parse(
        subscriptionListSchema,
        await api.request<unknown>(
          api.client.GET("/api/v1/monitor-subscriptions", {
            params: { query: { include_archived: false } },
          }),
        ),
        "INVALID_MONITOR_SUBSCRIPTIONS_RESPONSE",
      )

      const baseResults = await Promise.allSettled([
        api.request<unknown>(
          api.client.GET("/api/v1/watchlists", {
            params: { query: { include_archived: false } },
          }),
        ),
        api.request<unknown>(api.client.GET("/api/v1/positions")),
        api.request<unknown>(api.client.GET("/api/v1/monitor-schedules")),
        api.request<unknown>(
          api.client.GET("/api/v1/targets", {
            params: { query: { page: 1, page_size: 200 } },
          }),
        ),
        api.request<unknown>(
          api.client.GET("/api/v1/signals/states", {
            params: { query: { page: 1, page_size: 200 } },
          }),
        ),
      ])

      const warnings: string[] = []
      const watchlists = valueOrWarning(
        baseResults[0],
        watchlistListSchema,
        "WATCHLISTS_UNAVAILABLE",
        { items: [] },
        warnings,
      )
      const positions = valueOrWarning(
        baseResults[1],
        positionListSchema,
        "POSITIONS_UNAVAILABLE",
        { items: [] },
        warnings,
      )
      const schedules = valueOrWarning(
        baseResults[2],
        scheduleListSchema,
        "SCHEDULES_UNAVAILABLE",
        { items: [] },
        warnings,
      )
      const targets = valueOrWarning(
        baseResults[3],
        targetListSchema,
        "TARGETS_UNAVAILABLE",
        { items: [] },
        warnings,
      )
      const signals = valueOrWarning(
        baseResults[4],
        signalListSchema,
        "SIGNALS_UNAVAILABLE",
        { items: [] },
        warnings,
      )

      const enrichmentResults = await Promise.allSettled(
        subscriptions.items.flatMap((subscription) => [
          api.request<unknown>(
            api.client.GET("/api/v1/securities/{symbol}", {
              params: { path: { symbol: subscription.symbol } },
            }),
          ),
          api.request<unknown>(
            api.client.GET("/api/v1/monitor-subscriptions/{subscription_id}", {
              params: { path: { subscription_id: subscription.id } },
            }),
          ),
        ]),
      )

      const groupsBySymbol = new Map<string, string[]>()
      for (const watchlist of watchlists.items) {
        for (const item of watchlist.items) {
          const groups = groupsBySymbol.get(item.symbol) ?? []
          groups.push(watchlist.name)
          groupsBySymbol.set(item.symbol, groups)
        }
      }
      const heldSymbols = new Set(
        positions.items
          .filter((position) => position.status === "HOLDING")
          .map((position) => position.symbol),
      )
      const scheduleNames = new Map(
        schedules.items.map((schedule) => [schedule.id, schedule.name]),
      )
      const targetBySubscription = new Map(
        targets.items.map((target) => [target.subscription_id, target]),
      )
      const signalBySubscription = new Map(
        signals.items.map((signal) => [signal.subscription_id, signal]),
      )

      const items: MonitoringOverviewItem[] = subscriptions.items.map(
        (subscription, index) => {
          const itemWarnings: string[] = []
          const securityResult = enrichmentResults[index * 2]
          const detailResult = enrichmentResults[index * 2 + 1]
          const security = valueOrWarning(
            securityResult,
            securitySchema,
            "SECURITY_DETAIL_UNAVAILABLE",
            null,
            itemWarnings,
          )
          const detail = valueOrWarning(
            detailResult,
            subscriptionDetailSchema,
            "SUBSCRIPTION_DETAIL_UNAVAILABLE",
            null,
            itemWarnings,
          )
          const currentRevision = detail?.revisions.find(
            (revision) => revision.id === subscription.current_revision_id,
          ) ?? null
          const signal = signalBySubscription.get(subscription.id)
          const target = targetBySubscription.get(subscription.id)

          return {
            subscriptionId: subscription.id,
            symbol: subscription.symbol,
            securityName: security?.name ?? null,
            groups: groupsBySymbol.get(subscription.symbol) ?? [],
            isHolding: heldSymbols.has(subscription.symbol),
            subscriptionStatus: subscription.status,
            subscriptionVersion: subscription.version,
            scheduleName: currentRevision?.schedule_id
              ? (scheduleNames.get(currentRevision.schedule_id) ?? null)
              : null,
            targetMode: currentRevision?.target_mode ?? null,
            strategyVersionId: currentRevision?.strategy_version_id ?? null,
            targetStatus: target?.status ?? null,
            zone: signal?.zone ?? null,
            lastPrice: signal?.last_price ?? null,
            lastPriceAt: signal?.last_price_at ?? null,
            allowedActions: subscription.allowed_actions,
            warningCodes: itemWarnings,
          }
        },
      )

      return {
        generatedAt: new Date().toISOString(),
        items,
        warningCodes: [...new Set(warnings)],
      } satisfies MonitoringOverview
    },
    async runAction(subscriptionId, action, expectedVersion, reason) {
      const body = {
        expected_version: expectedVersion,
        reason,
        confirm: true as const,
      }
      await runSubscriptionAction(
        api,
        subscriptionId,
        action,
        body,
      )
    },
  }
}

export const monitoringGateway = createMonitoringGateway()

async function runSubscriptionAction(
  api: ReturnType<typeof createApiClient<paths>>,
  subscriptionId: string,
  action: MonitoringAction,
  body: {
    expected_version: number
    reason: string
    confirm: true
  },
) {
  const params = {
    params: {
      path: { subscription_id: subscriptionId },
      header: { "Idempotency-Key": createClientIdempotencyKey() },
    },
    body,
  }
  if (action === "ENABLE") {
    await api.request(api.client.POST(
      "/api/v1/monitor-subscriptions/{subscription_id}/enable",
      params,
    ))
    return
  }
  if (action === "DISABLE") {
    await api.request(api.client.POST(
      "/api/v1/monitor-subscriptions/{subscription_id}/disable",
      params,
    ))
    return
  }
  if (action === "ARCHIVE") {
    await api.request(api.client.POST(
      "/api/v1/monitor-subscriptions/{subscription_id}/archive",
      params,
    ))
    return
  }
  if (action === "RESTORE") {
    await api.request(api.client.POST(
      "/api/v1/monitor-subscriptions/{subscription_id}/restore",
      params,
    ))
    return
  }
  if (action === "CHECK_NOW") {
    await api.request(api.client.POST(
      "/api/v1/monitor-subscriptions/{subscription_id}/check-now",
      params,
    ))
    return
  }
  await api.request(api.client.POST(
    "/api/v1/monitor-subscriptions/{subscription_id}/diagnose",
    params,
  ))
}
