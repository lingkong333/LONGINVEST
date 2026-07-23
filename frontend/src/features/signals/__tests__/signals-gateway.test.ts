import { http, HttpResponse } from "msw"
import { setupServer } from "msw/node"
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest"

import { createSignalsGateway } from "@/features/signals"
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
    request_id: "req-signals",
    server_time: "2026-07-23T03:00:00Z",
  }
}

const state = {
  subscription_id: "sub-1",
  zone: "LOW",
  version: 3,
  last_price: "8.10",
  last_price_at: "2026-07-23T02:50:00Z",
}

const signalEvent = {
  id: "signal-event-1",
  subscription_id: "sub-1",
  evaluation_id: "evaluation-1",
  before_zone: "NORMAL",
  after_zone: "LOW",
  reason: "SCHEDULED_QUOTE",
  price: "8.10",
  price_at: "2026-07-23T02:50:00Z",
  targets: {
    low_strong: "7.00",
    low_watch: "8.20",
    high_watch: "12.00",
    high_strong: "13.00",
  },
  target_revision_id: "target-1",
  target_version: 2,
  target_date: "2026-07-22",
  position_status: "NOT_HOLDING",
  position_version: 1,
  used_stale_target: false,
  state_version: 3,
  notification_class: "LOW",
  notification_eligible: true,
  suppression_reason: null,
  created_at: "2026-07-23T02:50:01Z",
}

const evaluation = {
  id: "evaluation-1",
  subscription_id: "sub-1",
  reason: "SCHEDULED_QUOTE",
  result: "APPLIED",
  before_zone: "NORMAL",
  after_zone: "LOW",
  price: "8.10",
  price_at: "2026-07-23T02:50:00Z",
  hysteresis_applied: false,
  used_stale_target: false,
  skip_code: null,
  content_hash: "hash-1",
  created_at: "2026-07-23T02:50:01Z",
}

function useHandlers() {
  server.use(
    http.get("http://localhost/api/v1/signals/states", ({ request }) => {
      const url = new URL(request.url)
      expect(url.searchParams.get("page")).toBe("2")
      expect(url.searchParams.get("page_size")).toBe("20")
      return HttpResponse.json(envelope({
        items: [state],
        pagination: { page: 2, page_size: 20, total: 21 },
      }))
    }),
    http.get("http://localhost/api/v1/signal-events", () => (
      HttpResponse.json(envelope({
        items: [signalEvent],
        pagination: { page: 1, page_size: 20, total: 1 },
      }))
    )),
    http.get("http://localhost/api/v1/signal-evaluations", () => (
      HttpResponse.json(envelope({
        items: [evaluation],
        pagination: { page: 1, page_size: 20, total: 1 },
      }))
    )),
    http.get("http://localhost/api/v1/notifications/events", () => (
      HttpResponse.json(envelope({
        items: [{
          id: "notification-event-1",
          business_event_id: "signal-event-1",
          business_event_type: "signal.transitioned",
          status: "DELIVERED",
        }],
        page: 1,
        page_size: 200,
        total: 1,
      }))
    )),
    http.get("http://localhost/api/v1/notifications/deliveries", () => (
      HttpResponse.json(envelope({
        items: [{
          id: "delivery-1",
          event_id: "notification-event-1",
          channel: "WECOM",
          status: "SENT",
          sent_at: "2026-07-23T02:50:05Z",
          error_code: null,
        }],
        page: 1,
        page_size: 200,
        total: 1,
      }))
    )),
  )
}

describe("信号中心请求边界", () => {
  it("读取分页状态和判断记录，并校验响应结构", async () => {
    useHandlers()
    const gateway = createSignalsGateway("http://localhost")

    const [states, evaluations] = await Promise.all([
      gateway.loadStates(2, 20),
      gateway.loadEvaluations(1, 20),
    ])

    expect(states).toEqual({
      items: [expect.objectContaining({ zone: "LOW", last_price: "8.10" })],
      page: 2,
      pageSize: 20,
      total: 21,
    })
    expect(evaluations.items[0]).toEqual(expect.objectContaining({
      result: "APPLIED",
      before_zone: "NORMAL",
      after_zone: "LOW",
    }))
  })

  it("把信号事件与通知投递结果关联", async () => {
    useHandlers()
    const gateway = createSignalsGateway("http://localhost")

    const events = await gateway.loadEvents(1, 20)

    expect(events.warningCodes).toEqual([])
    expect(events.items[0]).toEqual(expect.objectContaining({
      id: "signal-event-1",
      notificationStatus: "DELIVERED",
      deliveries: [{
        id: "delivery-1",
        channel: "WECOM",
        status: "SENT",
        sentAt: "2026-07-23T02:50:05Z",
        errorCode: null,
      }],
    }))
  })

  it("通知接口失败时保留信号事件并标记局部降级", async () => {
    useHandlers()
    server.use(
      http.get("http://localhost/api/v1/notifications/deliveries", () => (
        HttpResponse.json({
          ...envelope(null),
          success: false,
          code: "NOTIFICATIONS_UNAVAILABLE",
        }, { status: 503 })
      )),
    )
    const gateway = createSignalsGateway("http://localhost")

    const events = await gateway.loadEvents(1, 20)

    expect(events.items).toHaveLength(1)
    expect(events.warningCodes).toContain("NOTIFICATION_DELIVERIES_UNAVAILABLE")
  })

  it("核心信号响应结构异常时拒绝展示", async () => {
    useHandlers()
    server.use(
      http.get("http://localhost/api/v1/signals/states", () => (
        HttpResponse.json(envelope({ items: "invalid" }))
      )),
    )
    const gateway = createSignalsGateway("http://localhost")

    await expect(gateway.loadStates(2, 20)).rejects.toMatchObject({
      code: "INVALID_SIGNAL_STATE_RESPONSE",
    })
  })
})
