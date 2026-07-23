import { http, HttpResponse } from "msw"
import { setupServer } from "msw/node"
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest"

import { createAlertGateway } from "@/features/alerts"
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
    request_id: "req-alert",
    server_time: "2026-07-23T03:00:00Z",
  }
}

const alert = {
  id: "alert-1",
  aggregation_key: "worker:worker-1",
  alert_type: "WORKER_TIMEOUT",
  object_type: "worker",
  object_id: "worker-1",
  severity: "ERROR",
  status: "OPEN",
  title: "行情进程超时",
  summary: "行情进程已超过心跳期限",
  details: { worker: "worker-1" },
  occurrence_count: 2,
  first_seen_at: "2026-07-23T01:00:00Z",
  last_seen_at: "2026-07-23T02:00:00Z",
  acknowledged_at: null,
  acknowledged_by_user_id: null,
  resolved_at: null,
  resolved_by_user_id: null,
  resolution_reason: null,
  version: 3,
  created_at: "2026-07-23T01:00:00Z",
  updated_at: "2026-07-23T02:00:00Z",
  allowed_actions: ["ACKNOWLEDGE", "RESOLVE", "RETRY"],
}

describe("系统告警请求边界", () => {
  it("读取筛选分页并保留后端允许操作", async () => {
    server.use(
      http.get("http://localhost/api/v1/alerts", ({ request }) => {
        const url = new URL(request.url)
        expect(url.searchParams.get("page")).toBe("2")
        expect(url.searchParams.get("page_size")).toBe("20")
        expect(url.searchParams.get("status")).toBe("OPEN")
        expect(url.searchParams.get("severity")).toBe("ERROR")
        expect(url.searchParams.get("alert_type")).toBe("WORKER_TIMEOUT")
        return HttpResponse.json(envelope({
          items: [alert],
          total: 21,
          page: 2,
          page_size: 20,
        }))
      }),
    )

    const result = await createAlertGateway("http://localhost").loadAlerts({
      page: 2,
      pageSize: 20,
      status: "OPEN",
      severity: "ERROR",
      alertType: "WORKER_TIMEOUT",
    })

    expect(result.total).toBe(21)
    expect(result.items[0]).toEqual(expect.objectContaining({
      id: "alert-1",
      status: "OPEN",
      allowedActions: ["ACKNOWLEDGE", "RESOLVE", "RETRY"],
    }))
  })

  it("后端没有返回允许操作时保持全部禁用", async () => {
    const withoutAllowedActions = { ...alert, allowed_actions: undefined }
    server.use(
      http.get("http://localhost/api/v1/alerts/alert-1", () => (
        HttpResponse.json(envelope(withoutAllowedActions))
      )),
    )

    const result = await createAlertGateway("http://localhost").loadAlert("alert-1")

    expect(result.allowedActions).toEqual([])
  })

  it("读取发生记录和永久处理历史", async () => {
    server.use(
      http.get("http://localhost/api/v1/alerts/alert-1/occurrences", () => (
        HttpResponse.json(envelope({
          items: [{
            id: "occurrence-1",
            alert_id: "alert-1",
            source_event_id: "event-1",
            severity: "ERROR",
            summary: "再次超时",
            details: {},
            request_id: "req-occurrence",
            occurred_at: "2026-07-23T02:00:00Z",
          }],
          total: 1,
          page: 1,
          page_size: 50,
        }))
      )),
      http.get("http://localhost/api/v1/alerts/alert-1/actions", () => (
        HttpResponse.json(envelope({
          items: [{
            id: "action-1",
            alert_id: "alert-1",
            action: "OPENED",
            reason: null,
            actor_user_id: null,
            request_id: "req-action",
            job_id: null,
            created_at: "2026-07-23T01:00:00Z",
          }],
          total: 1,
          page: 1,
          page_size: 50,
        }))
      )),
    )
    const gateway = createAlertGateway("http://localhost")

    const [occurrences, actions] = await Promise.all([
      gateway.loadOccurrences("alert-1"),
      gateway.loadActions("alert-1"),
    ])

    expect(occurrences.items[0].sourceEventId).toBe("event-1")
    expect(actions.items[0].action).toBe("OPENED")
  })

  it("重试只提交后台任务请求并返回任务编号", async () => {
    let body: unknown
    let idempotencyKey: string | null = null
    server.use(
      http.post("http://localhost/api/v1/alerts/alert-1/retry", async ({ request }) => {
        body = await request.json()
        idempotencyKey = request.headers.get("Idempotency-Key")
        return HttpResponse.json(envelope({
          alert: { ...alert, version: 4 },
          job_id: "job-1",
          replayed: false,
        }), { status: 202 })
      }),
    )

    const result = await createAlertGateway("http://localhost").runAction({
      alertId: "alert-1",
      action: "RETRY",
      expectedVersion: 3,
      reason: "确认故障仍然存在",
    })

    expect(body).toEqual({
      expected_version: 3,
      reason: "确认故障仍然存在",
      confirm: true,
    })
    expect(idempotencyKey).toBeTruthy()
    expect(result.jobId).toBe("job-1")
  })
})
