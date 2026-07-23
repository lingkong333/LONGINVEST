import { http, HttpResponse } from "msw"
import { setupServer } from "msw/node"
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest"

import { createMonitoringGateway } from "@/features/monitoring"
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
    request_id: "req-monitoring",
    server_time: "2026-07-23T02:00:00Z",
  }
}

const subscription = {
  id: "sub-1",
  security_id: "security-1",
  symbol: "600000.SH",
  status: "ENABLED",
  version: 3,
  current_revision_id: "revision-1",
  archived_at: null,
}

function useHappyPathHandlers() {
  server.use(
    http.get("http://localhost/api/v1/monitor-subscriptions", () => (
      HttpResponse.json(envelope({ items: [subscription] }))
    )),
    http.get("http://localhost/api/v1/watchlists", () => (
      HttpResponse.json(envelope({
        items: [{
          id: "watchlist-1",
          owner_user_id: "user-1",
          name: "核心观察",
          description: null,
          display_order: 0,
          version: 1,
          archived: false,
          items: [{
            id: "item-1",
            watchlist_id: "watchlist-1",
            security_id: "security-1",
            symbol: "600000.SH",
          }],
        }],
      }))
    )),
    http.get("http://localhost/api/v1/positions", () => (
      HttpResponse.json(envelope({
        items: [{
          security_id: "security-1",
          symbol: "600000.SH",
          status: "HOLDING",
          version: 2,
        }],
      }))
    )),
    http.get("http://localhost/api/v1/monitor-schedules", () => (
      HttpResponse.json(envelope({
        items: [{
          id: "schedule-1",
          name: "早盘",
          current_revision_id: "schedule-revision-1",
          version: 1,
          archived_at: null,
        }],
      }))
    )),
    http.get("http://localhost/api/v1/targets", () => (
      HttpResponse.json(envelope({
        items: [{
          subscription_id: "sub-1",
          status: "READY",
        }],
        pagination: { page: 1, page_size: 200, total: 1 },
      }))
    )),
    http.get("http://localhost/api/v1/signals/states", () => (
      HttpResponse.json(envelope({
        items: [{
          subscription_id: "sub-1",
          zone: "NORMAL",
          version: 1,
          last_price: "10.25",
          last_price_at: "2026-07-23T01:45:00Z",
        }],
        pagination: { page: 1, page_size: 200, total: 1 },
      }))
    )),
    http.get("http://localhost/api/v1/securities/600000.SH", () => (
      HttpResponse.json(envelope({
        symbol: "600000.SH",
        name: "浦发银行",
      }))
    )),
    http.get("http://localhost/api/v1/monitor-subscriptions/sub-1", () => (
      HttpResponse.json(envelope({
        subscription,
        revisions: [{
          id: "revision-1",
          subscription_id: "sub-1",
          revision_no: 1,
          schedule_id: "schedule-1",
          schedule_revision_id: "schedule-revision-1",
          target_mode: "STRATEGY",
          target_version_id: "target-1",
          strategy_version_id: "strategy-version-1",
          parameters: {},
          hysteresis_ratio: "0",
          hysteresis_min: "0",
          notification_mode: "INHERIT",
          notification_channels: [],
          reason: "创建",
        }],
      }))
    )),
  )
}

describe("监控列表请求边界", () => {
  it("合并订阅、分组、持仓、目标、信号和调度数据", async () => {
    useHappyPathHandlers()
    const gateway = createMonitoringGateway("http://localhost")

    const result = await gateway.loadOverview()

    expect(result.warningCodes).toEqual([])
    expect(result.items).toEqual([
      expect.objectContaining({
        symbol: "600000.SH",
        securityName: "浦发银行",
        groups: ["核心观察"],
        isHolding: true,
        scheduleName: "早盘",
        targetMode: "STRATEGY",
        targetStatus: "READY",
        zone: "NORMAL",
        lastPrice: "10.25",
      }),
    ])
  })

  it("辅助接口失败只标记降级，不丢失核心订阅", async () => {
    useHappyPathHandlers()
    server.use(
      http.get("http://localhost/api/v1/positions", () => (
        HttpResponse.json({
          ...envelope(null),
          success: false,
          code: "POSITIONS_UNAVAILABLE",
        }, { status: 503 })
      )),
      http.get("http://localhost/api/v1/securities/600000.SH", () => (
        HttpResponse.json({
          ...envelope(null),
          success: false,
          code: "SECURITY_UNAVAILABLE",
        }, { status: 503 })
      )),
    )
    const gateway = createMonitoringGateway("http://localhost")

    const result = await gateway.loadOverview()

    expect(result.items).toHaveLength(1)
    expect(result.items[0].isHolding).toBe(false)
    expect(result.items[0].warningCodes).toContain("SECURITY_UNAVAILABLE")
    expect(result.warningCodes).toContain("POSITIONS_UNAVAILABLE")
  })
})
