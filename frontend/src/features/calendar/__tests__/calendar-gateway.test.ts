import { afterEach, describe, expect, it, vi } from "vitest"

import {
  createCalendarGateway,
  parseCalendarImportFile,
} from "@/features/calendar"

function envelope(data: unknown) {
  return new Response(JSON.stringify({
    success: true,
    code: "OK",
    message: "成功",
    data,
    request_id: "req-calendar",
    server_time: "2026-07-23T08:00:00Z",
  }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  })
}

const day = {
  trade_date: "2026-07-23",
  is_trading_day: true,
  status: "CONFIRMED",
  source: "SSE",
  note: null,
  override_reason: null,
  sessions: [{ starts_at: "09:30:00", ends_at: "11:30:00" }],
  allowed_actions: ["OVERRIDE"],
}
const version = {
  id: "00000000-0000-4000-8000-000000000003",
  market: "CN_A",
  version_number: 3,
  source: "SSE",
  source_version: "2026",
  based_on_version_id: null,
  reason: "年度日历",
  created_at: "2026-01-01T00:00:00Z",
  is_current: true,
  allowed_actions: [],
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe("交易日历接口适配", () => {
  it("并行读取日期、覆盖和版本，并转换为页面模型", async () => {
    const fetchMock = vi.fn(async (request: Request) => {
      const path = new URL(request.url).pathname
      if (path.endsWith("/coverage")) {
        return envelope({
          market: "CN_A",
          from_date: "2026-07-01",
          confirmed_through: "2026-12-31",
          future_confirmed_days: 161,
          level: "OK",
          current_version_id: version.id,
          missing_today: false,
          allowed_actions: ["IMPORT", "OVERRIDE"],
        })
      }
      if (path.endsWith("/versions")) {
        return envelope({
          items: [version],
          allowed_actions: ["IMPORT", "OVERRIDE"],
        })
      }
      return envelope({
        items: [day],
        allowed_actions: ["IMPORT", "OVERRIDE"],
      })
    })
    vi.stubGlobal("fetch", fetchMock)

    const snapshot = await createCalendarGateway("https://calendar.test")
      .loadSnapshot("2026-07-01", "2026-07-31")

    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(snapshot.days[0]).toMatchObject({
      tradeDate: "2026-07-23",
      isTradingDay: true,
      allowedActions: ["OVERRIDE"],
    })
    expect(snapshot.coverage.futureConfirmedDays).toBe(161)
    expect(snapshot.versions[0]).toMatchObject({
      versionNumber: 3,
      isCurrent: true,
    })
    expect(snapshot.allowedActions).toEqual(["IMPORT", "OVERRIDE"])
  })

  it("单日覆盖只提交受控字段、确认和当前版本", async () => {
    const fetchMock = vi.fn().mockResolvedValue(envelope({ created: true }))
    vi.stubGlobal("fetch", fetchMock)
    const gateway = createCalendarGateway("https://calendar.test")

    await gateway.overrideDay({
      day: {
        tradeDate: "2026-07-23",
        isTradingDay: true,
        status: "CONFIRMED",
        source: "SSE",
        note: null,
        overrideReason: null,
        sessions: [],
        allowedActions: ["OVERRIDE"],
      },
      isTradingDay: false,
      expectedCurrentVersion: 3,
      reason: "临时休市",
      note: "台风",
    })

    const request = fetchMock.mock.calls[0][0] as Request
    expect(request.method).toBe("PATCH")
    expect(await request.json()).toEqual({
      market: "CN_A",
      is_trading_day: false,
      expected_current_version: 3,
      reason: "临时休市",
      confirm: true,
      note: "台风",
    })
    expect(request.headers.get("Idempotency-Key")).toBeTruthy()
  })

  it("导入文件拒绝空日期和额外危险字段", () => {
    expect(() => parseCalendarImportFile({
      market: "CN_A",
      source: "SSE",
      source_version: "2026",
      days: [],
    })).toThrow("文件内容不是有效的交易日历格式")
    expect(() => parseCalendarImportFile({
      market: "CN_A",
      source: "SSE",
      source_version: "2026",
      days: [{
        trade_date: "2026-07-23",
        is_trading_day: true,
        status: "CONFIRMED",
        url: "https://example.invalid",
      }],
    })).toThrow()
  })
})
