import { z } from "zod"

import type {
  MonitoringAction,
  MonitoringGateway,
  MonitoringOverview,
  MonitoringOverviewItem,
} from "@/features/monitoring/types"
import { ApiError, createApiClient, createClientRequestId } from "@/shared/api/client"
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
  })),
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
      header: { "Idempotency-Key": createClientRequestId() },
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
