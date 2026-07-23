import { http, HttpResponse } from "msw"
import { setupServer } from "msw/node"
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest"

import { createAuditGateway } from "@/features/audit/gateway"
import type { ApiEnvelope } from "@/shared/api/client"

const server = setupServer()
const baseUrl = "http://localhost"

beforeAll(() => server.listen({ onUnhandledRequest: "error" }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

function envelope<T>(data: T): ApiEnvelope<T> {
  return {
    success: true,
    code: "OK",
    message: "操作成功",
    data,
    request_id: "req-audit-page",
    server_time: "2026-07-23T05:00:00Z",
  }
}

const event = {
  id: "00000000-0000-4000-8000-000000000001",
  occurred_at: "2026-07-23T04:00:00Z",
  actor_user_id: "user-1",
  session_id: "session-1",
  trusted_ip: "127.0.0.1",
  action_code: "TARGET_UPDATE",
  object_type: "target",
  object_id: "target-1",
  result: "SUCCESS",
  before_summary: { price: "10.00" },
  after_summary: { price: "11.00" },
  reason: "人工调整",
  request_id: "req-1",
  idempotency_key: "idem-1",
  risk_level: "HIGH",
}

describe("审计记录请求边界", () => {
  it("提交服务端筛选和分页并解析安全字段", async () => {
    server.use(http.get(`${baseUrl}/api/v1/audit-events`, ({ request }) => {
      const search = new URL(request.url).searchParams
      expect(search.get("page")).toBe("2")
      expect(search.get("page_size")).toBe("20")
      expect(search.get("actor_user_id")).toBe("user-1")
      expect(search.get("action_code")).toBe("TARGET_UPDATE")
      expect(search.get("object_type")).toBe("target")
      expect(search.get("object_id")).toBe("target-1")
      expect(search.get("result")).toBe("SUCCESS")
      expect(search.get("risk_level")).toBe("HIGH")
      expect(search.get("request_id")).toBe("req-1")
      return HttpResponse.json(envelope({
        items: [event],
        pagination: { page: 2, page_size: 20, total: 25 },
        allowed_actions: [],
      }))
    }))

    const result = await createAuditGateway(baseUrl).loadEvents({
      page: 2,
      pageSize: 20,
      actorUserId: "user-1",
      actionCode: "TARGET_UPDATE",
      objectType: "target",
      objectId: "target-1",
      result: "SUCCESS",
      riskLevel: "HIGH",
      requestId: "req-1",
    })

    expect(result.allowedActions).toEqual([])
    expect(result.pagination).toEqual({ page: 2, pageSize: 20, total: 25 })
    expect(result.items[0]).toEqual(expect.objectContaining({
      actionCode: "TARGET_UPDATE",
      beforeSummary: { price: "10.00" },
      afterSummary: { price: "11.00" },
    }))
  })

  it("拒绝服务器返回任何审计操作权限", async () => {
    server.use(http.get(`${baseUrl}/api/v1/audit-events`, () => (
      HttpResponse.json(envelope({
        items: [event],
        pagination: { page: 1, page_size: 20, total: 1 },
        allowed_actions: ["DELETE"],
      }))
    )))

    await expect(createAuditGateway(baseUrl).loadEvents({
      page: 1,
      pageSize: 20,
    })).rejects.toMatchObject({ code: "INVALID_AUDIT_EVENTS" })
  })

  it("缺少 allowed_actions 时不静默推断权限", async () => {
    server.use(http.get(`${baseUrl}/api/v1/audit-events`, () => (
      HttpResponse.json(envelope({
        items: [],
        pagination: { page: 1, page_size: 20, total: 0 },
      }))
    )))

    await expect(createAuditGateway(baseUrl).loadEvents({
      page: 1,
      pageSize: 20,
    })).rejects.toMatchObject({ code: "INVALID_AUDIT_EVENTS" })
  })
})
