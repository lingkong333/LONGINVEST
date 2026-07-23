import { z } from "zod"

import type { DashboardGateway } from "@/features/dashboard/types"
import { ApiError, createApiClient } from "@/shared/api/client"
import type { paths } from "@/shared/api/generated/schema"

const sectionStatusSchema = z.enum([
  "OK",
  "EMPTY",
  "WAITING",
  "NON_TRADING_DAY",
  "DEGRADED",
  "ERROR",
  "TIMEOUT",
])

const sectionSchema = z.object({
  status: sectionStatusSchema,
  updated_at: z.string().min(1),
  data: z.record(z.string(), z.unknown()),
  error: z.string().nullable(),
})

const summarySchema = z.object({
  status: z.enum(["HEALTHY", "DEGRADED", "UNHEALTHY"]),
  generated_at: z.string().min(1),
  sections: z.object({
    system: sectionSchema,
    quote_batches: sectionSchema,
    monitoring: sectionSchema,
    positions: sectionSchema,
    signals: sectionSchema,
    daily_data: sectionSchema,
    targets: sectionSchema,
    jobs: sectionSchema,
    notifications: sectionSchema,
    providers: sectionSchema,
    infrastructure: sectionSchema,
    alerts: sectionSchema,
  }),
})

export function createDashboardGateway(baseUrl = ""): DashboardGateway {
  const api = createApiClient<paths>({ baseUrl })

  return {
    async loadSummary() {
      const value = await api.request<unknown>(
        api.client.GET("/api/v1/dashboard/summary"),
      )
      const parsed = summarySchema.safeParse(value)
      if (!parsed.success) {
        throw new ApiError("仪表盘响应结构无效。", {
          code: "INVALID_DASHBOARD_RESPONSE",
          cause: parsed.error,
        })
      }
      return parsed.data
    },
  }
}

export const dashboardGateway = createDashboardGateway()
