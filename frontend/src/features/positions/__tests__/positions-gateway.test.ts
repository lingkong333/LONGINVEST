import { http, HttpResponse } from "msw"
import { setupServer } from "msw/node"
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest"

import { createPositionGateway } from "@/features/positions"
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
    request_id: "req-position",
    server_time: "2026-07-23T03:00:00Z",
  }
}

const position = {
  security_id: "security-1",
  symbol: "600000.SH",
  status: "HOLDING",
  version: 3,
  source: "manual",
  updated_at: "2026-07-23T02:00:00Z",
  allowed_actions: ["CLEAR"],
}

function useReadHandlers() {
  server.use(
    http.get("http://localhost/api/v1/positions", () => (
      HttpResponse.json(envelope({ items: [position] }))
    )),
    http.get("http://localhost/api/v1/monitor-subscriptions", () => (
      HttpResponse.json(envelope({
        items: [{
          symbol: "600519.SH",
          status: "ENABLED",
        }],
      }))
    )),
    http.get("http://localhost/api/v1/securities/600000.SH", () => (
      HttpResponse.json(envelope({
        symbol: "600000.SH",
        name: "浦发银行",
      }))
    )),
    http.get("http://localhost/api/v1/position-history", () => (
      HttpResponse.json(envelope({
        items: [{
          id: "history-1",
          security_id: "security-1",
          symbol: "600000.SH",
          before_status: "NOT_HOLDING",
          after_status: "HOLDING",
          version: 3,
          note: "重新建仓",
          source: "manual",
          request_id: "req-history",
          effective_at: "2026-07-23T02:00:00Z",
        }],
      }))
    )),
  )
}

describe("持仓请求边界", () => {
  it("读取当前状态、后端允许操作、股票名称和监控关系", async () => {
    useReadHandlers()
    const gateway = createPositionGateway("http://localhost")

    const result = await gateway.loadCurrent()

    expect(result).toEqual({
      warningCodes: [],
      items: [{
        securityId: "security-1",
        symbol: "600000.SH",
        securityName: "浦发银行",
        status: "HOLDING",
        version: 3,
        source: "manual",
        updatedAt: "2026-07-23T02:00:00Z",
        isMonitored: false,
        allowedActions: ["CLEAR"],
        warningCodes: [],
      }],
    })
  })

  it("监控接口失败时不会误判为未监控", async () => {
    useReadHandlers()
    server.use(
      http.get("http://localhost/api/v1/monitor-subscriptions", () => (
        HttpResponse.json({
          ...envelope(null),
          success: false,
          code: "SUBSCRIPTIONS_UNAVAILABLE",
        }, { status: 503 })
      )),
    )
    const gateway = createPositionGateway("http://localhost")

    const result = await gateway.loadCurrent()

    expect(result.items[0].isMonitored).toBeNull()
    expect(result.warningCodes).toEqual(["SUBSCRIPTIONS_UNAVAILABLE"])
  })

  it("读取不可变持仓历史", async () => {
    useReadHandlers()
    const gateway = createPositionGateway("http://localhost")

    const result = await gateway.loadHistory()

    expect(result).toEqual([
      expect.objectContaining({
        id: "history-1",
        symbol: "600000.SH",
        beforeStatus: "NOT_HOLDING",
        afterStatus: "HOLDING",
        note: "重新建仓",
      }),
    ])
  })

  it("单只修改携带原因、备注、版本和幂等键", async () => {
    let receivedBody: unknown
    let idempotencyKey: string | null = null
    server.use(
      http.post(
        "http://localhost/api/v1/positions/600000.SH/clear",
        async ({ request }) => {
          receivedBody = await request.json()
          idempotencyKey = request.headers.get("Idempotency-Key")
          return HttpResponse.json(envelope({
            code: "POSITION_CHANGED",
            replayed: false,
            position: { ...position, status: "NOT_HOLDING", version: 4 },
          }))
        },
      ),
    )
    const gateway = createPositionGateway("http://localhost")

    await gateway.changePosition({
      symbol: "600000.SH",
      action: "CLEAR",
      expectedVersion: 3,
      reason: "已经卖出",
      note: "不记录成交数量",
    })

    expect(receivedBody).toEqual({
      expected_version: 3,
      reason: "已经卖出",
      note: "不记录成交数量",
      source: "manual",
    })
    expect(idempotencyKey).toBeTruthy()
  })

  it("批量修改逐只携带版本并保留部分失败结果", async () => {
    server.use(
      http.post("http://localhost/api/v1/positions/batch", async ({ request }) => {
        expect(await request.json()).toEqual({
          items: [
            {
              symbol: "600000.SH",
              target: "NOT_HOLDING",
              expected_version: 3,
              note: null,
            },
            {
              symbol: "600519.SH",
              target: "NOT_HOLDING",
              expected_version: 2,
              note: null,
            },
          ],
          reason: "批量核对",
          source: "manual",
        })
        return HttpResponse.json(envelope({
          items: [
            { symbol: "600000.SH", status: "CHANGED", code: "POSITION_CHANGED" },
            { symbol: "600519.SH", status: "REJECTED", code: "POSITION_VERSION_CONFLICT" },
          ],
        }))
      }),
    )
    const gateway = createPositionGateway("http://localhost")

    const result = await gateway.changeBatch({
      items: [
        { symbol: "600000.SH", action: "CLEAR", expectedVersion: 3 },
        { symbol: "600519.SH", action: "CLEAR", expectedVersion: 2 },
      ],
      reason: "批量核对",
      note: null,
    })

    expect(result).toEqual([
      { symbol: "600000.SH", status: "CHANGED", code: "POSITION_CHANGED" },
      { symbol: "600519.SH", status: "REJECTED", code: "POSITION_VERSION_CONFLICT" },
    ])
  })
})
