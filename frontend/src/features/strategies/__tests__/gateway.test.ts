import { describe, expect, it } from "vitest"

import type { paths } from "@/shared/api/generated/schema"
import { createApiClient } from "@/shared/api/client"
import { createStrategyApi, strategyGatewayInternals } from "@/features/strategies/gateway"

function envelope(data: unknown, success = true, code = "OK", message = "ok") {
  return {
    success,
    code,
    message,
    data,
    request_id: "request-1",
    server_time: "2026-07-23T00:00:00Z",
  }
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  })
}

describe("策略接口映射", () => {
  it("只采用服务端返回的允许动作", () => {
    const mapped = strategyGatewayInternals.draft(
      {
        id: "draft-1",
        strategy_id: "strategy-1",
        draft_version: 3,
        source_code: "def calculate_targets(): pass",
        metadata: { description: "服务器说明", category: "长波段" },
        parameter_schema: { type: "object" },
      },
      {
        id: "strategy-1",
        name: "长波段",
        version: 7,
        allowed_actions: ["SAVE_DRAFT", "RESTORE_REVISION", "VALIDATE", "ARCHIVE", "UNKNOWN"],
      },
    )

    expect(mapped).toMatchObject({
      strategyVersion: 7,
      allowedActions: ["validate", "archive"],
      canSave: true,
      canRestoreRevision: true,
      description: "服务器说明",
      metadata: { description: "服务器说明", category: "长波段" },
      parameterSchema: '{\n  "type": "object"\n}',
    })
  })

  it("回测任务操作也只采用服务端允许动作", () => {
    const mapped = strategyGatewayInternals.taskItem({
      task_id: "task-1",
      mode: "SINGLE",
      status: "PAUSED",
      date_range: {
        training_start_date: "2010-01-01",
        training_end_date: "2020-12-31",
        test_start_date: "2021-01-01",
        test_end_date: "2022-12-31",
      },
      item: {
        item_id: "item-1",
        security_id: "security-1",
        symbol: "600000.SH",
        name: "浦发银行",
        status: "FROZEN",
        attempt_count: 1,
      },
      allowed_actions: ["RESUME", "CANCEL", "DELETE"],
      created_at: "2026-07-23T00:00:00Z",
      updated_at: "2026-07-23T00:01:00Z",
    })

    expect(mapped.allowedActions).toEqual(["RESUME", "CANCEL"])
    expect(mapped.dateRange).toEqual({
      trainingStartDate: "2010-01-01",
      trainingEndDate: "2020-12-31",
      testStartDate: "2021-01-01",
      testEndDate: "2022-12-31",
    })
  })

  it("父任务状态与单股状态分开映射，并保留数值结果", () => {
    const mapped = strategyGatewayInternals.result({
      task_id: "task-1",
      item_id: "item-1",
      item_status: "SUCCEEDED",
      adjustments: [],
      orders: [],
      trades: [],
      daily_results: [],
      metric: {
        item_id: "item-1",
        ending_equity: 112345.67,
        total_return: 0.1234567,
        realized_return: 0.1,
        annualized_return: 0.05,
        max_drawdown: 0.08,
        volatility: 0.12,
        completed_round_trips: 2,
        winning_trades: 1,
        losing_trades: 1,
        breakeven_trades: 0,
        capital_exposure_ratio: 0.4,
        open_position_at_end: false,
        unfilled_order_count: 0,
        longest_holding_trade_days: 50,
      },
    }, "task-1", "SUCCEEDED")

    expect(mapped.status).toBe("SUCCEEDED")
    expect(mapped.item?.status).toBe("SUCCEEDED")
    expect(mapped.metrics?.endingEquity).toBe("112345.67")
    expect(mapped.metrics?.totalReturn).toBe("0.1234567")
  })

  it("策略任务列表按草稿和发布版本归属过滤", async () => {
    const fetchMock: typeof globalThis.fetch = async (input) => {
      const request = input instanceof Request ? input : new Request(input)
      const url = new URL(request.url)
      if (url.pathname === "/api/v1/strategies/strategy-1") {
        return jsonResponse(envelope({
          id: "strategy-1",
          name: "长波段",
          version: 1,
          allowed_actions: ["SAVE_DRAFT"],
        }))
      }
      if (url.pathname === "/api/v1/strategies/strategy-1/draft") {
        return jsonResponse(envelope({
          id: "draft-1",
          strategy_id: "strategy-1",
          draft_version: 2,
          source_code: "pass",
        }))
      }
      if (url.pathname === "/api/v1/strategies/strategy-1/versions") {
        return jsonResponse(envelope({ items: [{ id: "version-1" }], pagination: {} }))
      }
      if (url.pathname === "/api/v1/backtests") {
        const base = {
          mode: "SINGLE",
          status: "SUCCEEDED",
          date_range: {
            training_start_date: "2010-01-01",
            training_end_date: "2020-12-31",
            test_start_date: "2021-01-01",
            test_end_date: "2022-12-31",
          },
          item: {
            item_id: "item-1",
            security_id: "security-1",
            symbol: "600000.SH",
            name: "样例",
            status: "SUCCEEDED",
            attempt_count: 1,
          },
          allowed_actions: ["RERUN"],
          created_at: "2026-07-23T00:00:00Z",
          updated_at: "2026-07-23T00:00:00Z",
        }
        return jsonResponse(envelope({
          items: [
            { ...base, task_id: "draft-task", draft_id: "draft-1" },
            { ...base, task_id: "version-task", strategy_version_id: "version-1" },
            { ...base, task_id: "other-task", draft_id: "draft-other" },
          ],
          pagination: { page: 1, page_size: 200, total: 3 },
        }))
      }
      return jsonResponse(envelope(null, false, "NOT_FOUND", "not found"), 404)
    }
    const api = createStrategyApi(createApiClient<paths>({
      baseUrl: "http://localhost",
      fetch: fetchMock,
    }))

    const page = await api.listHoldoutBacktests("strategy-1")

    expect(page.items.map((item) => item.taskId)).toEqual(["draft-task", "version-task"])
    expect(page.total).toBe(2)
  })
})
