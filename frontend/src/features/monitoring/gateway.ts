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
    trigger_type: z.enum(["AUTOMATIC", "MANUAL"]),
    expected_count: z.number().int().nonnegative(),
    fetched_count: z.number().int().nonnegative(),
    failed_count: z.number().int().nonnegative(),
    started_at: z.string().nullable(),
    completed_at: z.string().nullable(),
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

function executionStatus(value: string) {
  if (value === "SUCCEEDED") return "SUCCEEDED" as const
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
      const [schedules, occurrenceValue] = await Promise.all([
        this.loadSchedules(),
        api.request<unknown>(api.client.GET("/api/v1/schedule-occurrences", {
          params: { query: { page: 1, page_size: 200, occurrence_type: "REALTIME_QUOTE", from_date: today, through_date: today } },
        })),
      ])
      const occurrences = parse(occurrencePageSchema, occurrenceValue, "INVALID_MONITOR_OCCURRENCES_RESPONSE").items
      const automaticByTime = new Map(
        occurrences
          .filter((occurrence) => occurrence.trigger_type === "AUTOMATIC")
          .map((occurrence) => [shanghaiTime(occurrence.scheduled_at), occurrence]),
      )
      const nowTime = shanghaiTime(new Date().toISOString())
      const times = [...new Set(schedules.flatMap((schedule) => schedule.times))].sort()
      const itemFromOccurrence = (occurrence: z.infer<typeof occurrencePageSchema>["items"][number]) => {
        const durationSeconds = occurrence.started_at && occurrence.completed_at
          ? Math.max(0, Math.round((new Date(occurrence.completed_at).getTime() - new Date(occurrence.started_at).getTime()) / 1000))
            : null
        return { executionId: occurrence.occurrence_id, scheduledTime: shanghaiTime(occurrence.scheduled_at), triggerType: occurrence.trigger_type, status: executionStatus(occurrence.status), expectedCount: occurrence.expected_count, fetchedCount: occurrence.fetched_count, failedCount: occurrence.failed_count, startedAt: occurrence.started_at, completedAt: occurrence.completed_at, durationSeconds }
      }
      const plannedItems = times.map((scheduledTime) => {
        const occurrence = automaticByTime.get(scheduledTime)
        if (occurrence) return itemFromOccurrence(occurrence)
        return { executionId: `planned:${scheduledTime}`, scheduledTime, triggerType: "AUTOMATIC" as const, status: scheduledTime > nowTime ? "PENDING" as const : "NOT_EXECUTED" as const, expectedCount: 0, fetchedCount: 0, failedCount: 0, startedAt: null, completedAt: null, durationSeconds: null }
      })
      const manualItems = occurrences
        .filter((occurrence) => occurrence.trigger_type === "MANUAL")
        .map(itemFromOccurrence)
      const items = [...plannedItems, ...manualItems].sort((left, right) => (
        left.scheduledTime.localeCompare(right.scheduledTime)
        || left.triggerType.localeCompare(right.triggerType)
      ))
      const hasAttention = items.some((item) => ["NOT_EXECUTED", "PARTIAL", "FAILED"].includes(item.status))
      const hasRunning = items.some((item) => item.status === "RUNNING")
      const hasSuccess = items.some((item) => item.status === "SUCCEEDED")
      return {
        overallStatus: times.length === 0 ? "NOT_CONFIGURED" : hasAttention ? "ATTENTION" : hasRunning ? "RUNNING" : hasSuccess ? "NORMAL" : "PENDING",
        plannedCount: times.length,
        executedCount: items.filter((item) => ["SUCCEEDED", "PARTIAL", "FAILED"].includes(item.status)).length,
        fetchedCount: items.reduce((total, item) => total + item.fetchedCount, 0),
        items,
      } satisfies MonitoringExecutionOverview
    },
    async triggerMarketSnapshot() {
      await api.request(api.client.POST("/api/v1/quotes/market-snapshot", {
        params: { header: { "Idempotency-Key": createClientIdempotencyKey() } },
        body: {
          timeout_seconds: 60,
          confirm: true,
          reason: "从监控页面手动验证全市场盘中快照",
        },
      }))
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
