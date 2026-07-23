import { z } from "zod"

import type {
  PositionAction,
  PositionBatchResult,
  PositionGateway,
  PositionHistoryItem,
  PositionOverview,
} from "@/features/positions/types"
import { ApiError, createApiClient, createClientRequestId } from "@/shared/api/client"
import type { paths } from "@/shared/api/generated/schema"

const positionActionSchema = z.enum(["HOLD", "CLEAR"])
const positionStatusSchema = z.enum(["HOLDING", "NOT_HOLDING"])

const positionSchema = z.object({
  security_id: z.string().min(1),
  symbol: z.string().min(1),
  status: positionStatusSchema,
  version: z.number().int().nonnegative(),
  source: z.string().nullable().optional(),
  updated_at: z.string().nullable().optional(),
  allowed_actions: z.array(positionActionSchema),
})

const positionListSchema = z.object({
  items: z.array(positionSchema),
})

const historyListSchema = z.object({
  items: z.array(z.object({
    id: z.string().min(1),
    symbol: z.string().min(1),
    before_status: positionStatusSchema.nullable(),
    after_status: positionStatusSchema,
    version: z.number().int().positive(),
    note: z.string().nullable(),
    source: z.string().min(1),
    request_id: z.string().min(1),
    effective_at: z.string().min(1),
  })),
})

const subscriptionListSchema = z.object({
  items: z.array(z.object({
    symbol: z.string().min(1),
    status: z.string().min(1),
  })),
})

const securitySchema = z.object({
  symbol: z.string().min(1),
  name: z.string().min(1),
})

const batchResultSchema = z.object({
  items: z.array(z.object({
    symbol: z.string().min(1),
    status: z.string().min(1),
    code: z.string().min(1),
  })),
})

function parse<T>(schema: z.ZodType<T>, value: unknown, code: string): T {
  const result = schema.safeParse(value)
  if (!result.success) {
    throw new ApiError("持仓接口返回的数据无法识别。", {
      code,
      cause: result.error,
    })
  }
  return result.data
}

function failureCode(error: unknown, fallback: string) {
  return error instanceof ApiError ? error.code : fallback
}

export function createPositionGateway(baseUrl = ""): PositionGateway {
  const api = createApiClient<paths>({ baseUrl })

  return {
    async loadCurrent() {
      const [positionsValue, subscriptionsResult] = await Promise.all([
        api.request<unknown>(api.client.GET("/api/v1/positions")),
        api.request<unknown>(
          api.client.GET("/api/v1/monitor-subscriptions", {
            params: { query: { include_archived: false } },
          }),
        ).then(
          (value) => ({ status: "fulfilled" as const, value }),
          (reason: unknown) => ({ status: "rejected" as const, reason }),
        ),
      ])
      const positions = parse(
        positionListSchema,
        positionsValue,
        "POSITION_LIST_INVALID",
      )
      const warningCodes: string[] = []
      let monitoredSymbols: Set<string> | null = null
      if (subscriptionsResult.status === "fulfilled") {
        const subscriptions = parse(
          subscriptionListSchema,
          subscriptionsResult.value,
          "POSITION_SUBSCRIPTIONS_INVALID",
        )
        monitoredSymbols = new Set(
          subscriptions.items
            .filter((item) => item.status !== "ARCHIVED")
            .map((item) => item.symbol),
        )
      } else {
        warningCodes.push(
          failureCode(subscriptionsResult.reason, "POSITION_SUBSCRIPTIONS_UNAVAILABLE"),
        )
      }

      const securityResults = await Promise.allSettled(
        positions.items.map((position) => (
          api.request<unknown>(
            api.client.GET("/api/v1/securities/{symbol}", {
              params: { path: { symbol: position.symbol } },
            }),
          )
        )),
      )

      const items = positions.items.map((position, index) => {
        const securityResult = securityResults[index]
        const itemWarnings: string[] = []
        let securityName: string | null = null
        if (securityResult.status === "fulfilled") {
          try {
            securityName = parse(
              securitySchema,
              securityResult.value,
              "POSITION_SECURITY_INVALID",
            ).name
          } catch (error) {
            itemWarnings.push(failureCode(error, "POSITION_SECURITY_INVALID"))
          }
        } else {
          itemWarnings.push(
            failureCode(securityResult.reason, "POSITION_SECURITY_UNAVAILABLE"),
          )
        }
        return {
          securityId: position.security_id,
          symbol: position.symbol,
          securityName,
          status: position.status,
          version: position.version,
          source: position.source ?? null,
          updatedAt: position.updated_at ?? null,
          isMonitored: monitoredSymbols ? monitoredSymbols.has(position.symbol) : null,
          allowedActions: position.allowed_actions,
          warningCodes: itemWarnings,
        }
      })

      return { items, warningCodes } satisfies PositionOverview
    },

    async loadHistory() {
      const value = await api.request<unknown>(
        api.client.GET("/api/v1/position-history"),
      )
      const history = parse(
        historyListSchema,
        value,
        "POSITION_HISTORY_INVALID",
      )
      return history.items.map((item) => ({
        id: item.id,
        symbol: item.symbol,
        beforeStatus: item.before_status,
        afterStatus: item.after_status,
        version: item.version,
        note: item.note,
        source: item.source,
        requestId: item.request_id,
        effectiveAt: item.effective_at,
      })) satisfies PositionHistoryItem[]
    },

    async changePosition(input) {
      const params = {
        params: {
          path: { symbol: input.symbol },
          header: { "Idempotency-Key": createClientRequestId() },
        },
        body: {
          expected_version: input.expectedVersion || null,
          reason: input.reason,
          note: input.note,
          source: "manual",
        },
      }
      if (input.action === "HOLD") {
        await api.request(api.client.POST(
          "/api/v1/positions/{symbol}/hold",
          params,
        ))
        return
      }
      await api.request(api.client.POST(
        "/api/v1/positions/{symbol}/clear",
        params,
      ))
    },

    async changeBatch(input) {
      const value = await api.request<unknown>(
        api.client.POST("/api/v1/positions/batch", {
          params: {
            header: { "Idempotency-Key": createClientRequestId() },
          },
          body: {
            items: input.items.map((item) => ({
              symbol: item.symbol,
              target: actionTarget(item.action),
              expected_version: item.expectedVersion || null,
              note: input.note,
            })),
            reason: input.reason,
            source: "manual",
          },
        }),
      )
      return parse(
        batchResultSchema,
        value,
        "POSITION_BATCH_RESULT_INVALID",
      ).items satisfies PositionBatchResult[]
    },
  }
}

function actionTarget(action: PositionAction) {
  return action === "HOLD" ? "HOLDING" as const : "NOT_HOLDING" as const
}

export const positionGateway = createPositionGateway()
