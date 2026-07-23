import { http, HttpResponse } from "msw"
import { setupServer } from "msw/node"
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest"

import { createSystemStatusGateway } from "@/features/system-status"
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
    request_id: "req-status",
    server_time: "2026-07-23T09:00:00Z",
  }
}

const component = {
  name: "PostgreSQL",
  category: "database",
  status: "HEALTHY",
  critical: true,
  source: "database",
  updated_at: "2026-07-23T09:00:00Z",
  message: null,
  details: [{ key: "latency", value: 12, unit: "ms" }],
}

describe("运行状态请求边界", () => {
  it("并行读取七个只读接口并转换公开字段", async () => {
    server.use(
      http.get("http://localhost/api/v1/system/health", () => HttpResponse.json(envelope({ status: "HEALTHY", updated_at: "2026-07-23T09:00:00Z", components: [component], allowed_actions: [] }))),
      http.get("http://localhost/api/v1/system/components", () => HttpResponse.json(envelope({ items: [component], allowed_actions: [] }))),
      http.get("http://localhost/api/v1/workers", () => HttpResponse.json(envelope({ items: [{ worker_id: "worker-1", queue: "realtime", status: "IDLE", current_job_id: null, started_at: null, heartbeat_at: "2026-07-23T09:00:00Z", processed_jobs: 20, failed_jobs: 1 }], allowed_actions: [] }))),
      http.get("http://localhost/api/v1/queues", () => HttpResponse.json(envelope({ items: [{ name: "realtime", status: "HEALTHY", depth: 2, active_workers: 1, oldest_job_at: null, updated_at: "2026-07-23T09:00:00Z" }], allowed_actions: [] }))),
      http.get("http://localhost/api/v1/scheduler/status", () => HttpResponse.json(envelope({ status: "HEALTHY", scan_interval_seconds: 10, last_scan_at: "2026-07-23T09:00:00Z", database_time: "2026-07-23T09:00:00Z", automatic_scheduling_paused: false, pause_reason: null, updated_at: "2026-07-23T09:00:00Z", allowed_actions: [] }))),
      http.get("http://localhost/api/v1/system-clock/status", () => HttpResponse.json(envelope({ status: "HEALTHY", application_time: "2026-07-23T09:00:00Z", database_time: "2026-07-23T09:00:00Z", max_skew_seconds: 0.2, automatic_scheduling_paused: false, sources: [], updated_at: "2026-07-23T09:00:00Z", allowed_actions: [] }))),
      http.get("http://localhost/api/v1/schedule-occurrences", ({ request }) => {
        const url = new URL(request.url)
        expect(url.searchParams.get("page")).toBe("1")
        expect(url.searchParams.get("page_size")).toBe("20")
        return HttpResponse.json(envelope({
          items: [{
            occurrence_id: "00000000-0000-4000-8000-000000000001",
            occurrence_type: "REALTIME_QUOTE",
            definition_id: "morning",
            scheduled_trade_date: "2026-07-23",
            scheduled_at: "2026-07-23T01:30:00Z",
            status: "DISPATCHED",
            job_id: null,
            missed_reason: null,
            created_at: "2026-07-23T01:30:00Z",
          }],
          pagination: { page: 1, page_size: 20, total: 1 },
          allowed_actions: [],
        }))
      }),
    )
    const gateway = createSystemStatusGateway("http://localhost")
    const [overall, components, runtime, scheduling, occurrences] = await Promise.all([
      gateway.loadOverall(),
      gateway.loadComponents(),
      gateway.loadRuntime(),
      gateway.loadScheduling(),
      gateway.loadOccurrences(),
    ])

    expect(overall).toEqual(expect.objectContaining({ status: "HEALTHY", componentCount: 1, allowedActions: [] }))
    expect(components[0].details[0]).toEqual({ key: "latency", value: "12", unit: "ms" })
    expect(runtime).toEqual(expect.objectContaining({ allowedActions: [], workers: [expect.objectContaining({ workerId: "worker-1" })] }))
    expect(scheduling.clock.maxSkewSeconds).toBe(0.2)
    expect(occurrences.items[0].occurrenceType).toBe("REALTIME_QUOTE")
  })

  it("拒绝缺失或非空的 allowed_actions", async () => {
    server.use(http.get("http://localhost/api/v1/system/health", () => HttpResponse.json(envelope({
      status: "HEALTHY",
      updated_at: "2026-07-23T09:00:00Z",
      components: [],
      allowed_actions: ["RESTART"],
    }))))

    await expect(createSystemStatusGateway("http://localhost").loadOverall())
      .rejects.toMatchObject({ code: "SYSTEM_HEALTH_INVALID" })
  })
})
