import { http, HttpResponse } from "msw"
import { setupServer } from "msw/node"
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest"

import { createDashboardGateway } from "@/features/dashboard"
import type { ApiEnvelope } from "@/shared/api/client"

const server = setupServer()

beforeAll(() => server.listen({ onUnhandledRequest: "error" }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

function envelope<T>(data: T): ApiEnvelope<T> {
  return {
    success: true,
    code: "OK",
    message: "OK",
    data,
    request_id: "req-dashboard",
    server_time: "2026-07-23T02:00:00Z",
  }
}

const section = {
  status: "OK",
  updated_at: "2026-07-23T02:00:00Z",
  data: {},
  error: null,
}

const summary = {
  status: "HEALTHY",
  generated_at: "2026-07-23T02:00:00Z",
  sections: {
    system: section,
    quote_batches: section,
    monitoring: section,
    positions: section,
    signals: section,
    daily_data: section,
    targets: section,
    jobs: section,
    notifications: section,
    providers: section,
    infrastructure: section,
    alerts: section,
  },
}

describe("仪表盘请求边界", () => {
  it("接受完整的十二分区响应", async () => {
    server.use(
      http.get("http://localhost/api/v1/dashboard/summary", () => (
        HttpResponse.json(envelope(summary))
      )),
    )

    const gateway = createDashboardGateway("http://localhost")

    await expect(gateway.loadSummary()).resolves.toEqual(summary)
  })

  it("拒绝缺少分区的响应，避免页面静默展示错误数据", async () => {
    const incompleteSections = { ...summary.sections }
    Reflect.deleteProperty(incompleteSections, "alerts")
    server.use(
      http.get("http://localhost/api/v1/dashboard/summary", () => (
        HttpResponse.json(envelope({
          ...summary,
          sections: incompleteSections,
        }))
      )),
    )

    const gateway = createDashboardGateway("http://localhost")

    await expect(gateway.loadSummary()).rejects.toMatchObject({
      code: "INVALID_DASHBOARD_RESPONSE",
    })
  })
})
