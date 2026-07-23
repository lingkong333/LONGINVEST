import { http, HttpResponse } from "msw"
import { setupServer } from "msw/node"
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest"

import { createProviderGateway } from "@/features/providers"
import type { ApiEnvelope } from "@/shared/api/client"

const server = setupServer()

beforeAll(() => server.listen({ onUnhandledRequest: "error" }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

function envelope<T>(data: T): ApiEnvelope<T> {
  return {
    success: true,
    code: "OK",
    message: "操作成功",
    data,
    request_id: "req-provider",
    server_time: "2026-07-23T08:00:00Z",
  }
}

describe("数据源请求边界", () => {
  it("严格转换数据源、健康和熔断许可", async () => {
    server.use(
      http.get("http://localhost/api/v1/providers", () => HttpResponse.json(envelope([{
        provider_code: "EASTMONEY",
        version: 3,
        reason: "调整限流",
        capabilities: [{
          capability: "REALTIME_QUOTE_BATCH",
          enabled: true,
          priority: 1,
          concurrency: 2,
          rate_per_second: 3,
          timeout_seconds: 5,
          auto_switch: true,
        }],
        allowed_actions: ["UPDATE_SETTINGS", "QUOTE_DIAGNOSTICS"],
      }]))),
      http.get("http://localhost/api/v1/providers/EASTMONEY/health", () => HttpResponse.json(envelope([{
        capability: "REALTIME_QUOTE_BATCH",
        status: "HEALTHY",
        consecutive_failures: 0,
        last_success_at: "2026-07-23T07:59:00Z",
        last_failure_at: null,
        metrics: { success_rate: 0.99, p95_latency_ms: 180 },
      }]))),
      http.get("http://localhost/api/v1/providers/circuits", () => HttpResponse.json(envelope([{
        id: "00000000-0000-4000-8000-000000000001",
        provider_code: "EASTMONEY",
        capability: "REALTIME_QUOTE_BATCH",
        state: "CLOSED",
        consecutive_failures: 0,
        cooldown_index: 0,
        opened_at: null,
        allowed_actions: ["PROBE"],
      }]))),
    )
    const gateway = createProviderGateway("http://localhost")

    const [providers, health, circuits] = await Promise.all([
      gateway.loadProviders(),
      gateway.loadHealth("EASTMONEY"),
      gateway.loadCircuits(),
    ])

    expect(providers[0]).toEqual(expect.objectContaining({
      code: "EASTMONEY",
      allowedActions: ["UPDATE_SETTINGS", "QUOTE_DIAGNOSTICS"],
    }))
    expect(health[0]).toEqual(expect.objectContaining({
      successRate: 0.99,
      p95LatencyMs: 180,
    }))
    expect(circuits[0].allowedActions).toEqual(["PROBE"])
  })

  it("许可字段缺失时不推断任何写操作", async () => {
    server.use(http.get("http://localhost/api/v1/providers", () => HttpResponse.json(envelope([{
      provider_code: "SINA",
      version: 1,
      reason: "初始配置",
      capabilities: [],
    }]))))

    const providers = await createProviderGateway("http://localhost").loadProviders()

    expect(providers[0].allowedActions).toEqual([])
  })

  it("配置、探测和诊断均提交确认、原因、版本和幂等键", async () => {
    const requests: { path: string; body: unknown; key: string | null }[] = []
    server.use(
      http.patch("http://localhost/api/v1/providers/EASTMONEY/settings", async ({ request }) => {
        requests.push({ path: "settings", body: await request.json(), key: request.headers.get("Idempotency-Key") })
        return HttpResponse.json(envelope({ version: 4, capabilities: [] }))
      }),
      http.post("http://localhost/api/v1/providers/circuits/:id/probe", async ({ request }) => {
        requests.push({ path: "probe", body: await request.json(), key: request.headers.get("Idempotency-Key") })
        return HttpResponse.json(envelope({ state: "CLOSED" }))
      }),
      http.post("http://localhost/api/v1/providers/quote-diagnostics", async ({ request }) => {
        requests.push({ path: "diagnostic", body: await request.json(), key: request.headers.get("Idempotency-Key") })
        return HttpResponse.json(envelope({
          symbols: ["600000.SH"],
          sources: [],
          comparisons: [{
            symbol: "600000.SH",
            status: "INCOMPLETE",
            missing_sources: ["EASTMONEY", "SINA"],
            differences: {},
          }],
        }))
      }),
    )
    const gateway = createProviderGateway("http://localhost")
    const provider = {
      code: "EASTMONEY" as const,
      version: 3,
      reason: "原配置",
      capabilities: [],
      allowedActions: ["UPDATE_SETTINGS" as const],
    }
    const circuit = {
      id: "00000000-0000-4000-8000-000000000001",
      providerCode: "EASTMONEY" as const,
      capability: "REALTIME_QUOTE_BATCH",
      state: "OPEN" as const,
      consecutiveFailures: 3,
      cooldownIndex: 1,
      openedAt: "2026-07-23T07:00:00Z",
      allowedActions: ["PROBE" as const],
    }

    await gateway.updateSettings({
      provider,
      settings: {
        enabled: true,
        priority: 1,
        concurrency: 2,
        ratePerSecond: 3,
        timeoutSeconds: 5,
        autoSwitch: true,
      },
      reason: "调整运行参数",
    })
    await gateway.runCircuitAction({ circuit, action: "PROBE", reason: "检查恢复" })
    await gateway.runQuoteDiagnostics(["600000.SH"], "对比来源")

    expect(requests.map((item) => item.body)).toEqual([
      {
        confirm: true,
        reason: "调整运行参数",
        expected_version: 3,
        enabled: true,
        priority: 1,
        concurrency: 2,
        rate_per_second: 3,
        timeout_seconds: 5,
        auto_switch: true,
      },
      { confirm: true, reason: "检查恢复" },
      { confirm: true, reason: "对比来源", symbols: ["600000.SH"] },
    ])
    expect(requests.every((item) => item.key?.startsWith("web_"))).toBe(true)
  })
})
