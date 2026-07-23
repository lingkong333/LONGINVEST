import { http, HttpResponse } from "msw"
import { setupServer } from "msw/node"
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest"

import { createMarketDataGateway } from "@/features/market-data"
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
    request_id: "req-market-data",
    server_time: "2026-07-23T03:00:00Z",
  }
}

const issue = {
  id: "00000000-0000-4000-8000-000000000001",
  issue_type: "SOURCE_CONFLICT",
  subject_type: "QUOTE",
  subject_id: "quote-1",
  symbol: "600000.SH",
  status: "OPEN",
  severity: "WARNING",
  evidence: {},
  source_candidates: ["tushare", "akshare"],
  allowed_actions: ["SELECT_SOURCE", "INVALIDATE", "REFETCH"],
  occurrence_count: 2,
  first_seen_at: "2026-07-23T02:00:00Z",
  last_seen_at: "2026-07-23T02:10:00Z",
  resolved_at: null,
  resolved_by_user_id: null,
  resolution_action: null,
  resolution_reason: null,
  selected_source: null,
}

describe("行情数据中心请求边界", () => {
  it("主数据刷新和行情任务提交人工原因", async () => {
    const received: unknown[] = []
    server.use(
      http.post(
        "http://localhost/api/v1/securities/refresh",
        async ({ request }) => {
          received.push(await request.json())
          return HttpResponse.json(envelope({
            job_id: "00000000-0000-4000-8000-000000000010",
            job_type: "SECURITY_MASTER_REFRESH",
            status: "PENDING_DISPATCH",
          }))
        },
      ),
      http.post(
        "http://localhost/api/v1/quote-cycles/manual",
        async ({ request }) => {
          received.push(await request.json())
          return HttpResponse.json(envelope({
            job_id: "00000000-0000-4000-8000-000000000011",
            status: "PENDING_DISPATCH",
          }))
        },
      ),
      http.post(
        "http://localhost/api/v1/quotes/diagnose",
        async ({ request }) => {
          received.push(await request.json())
          return HttpResponse.json(envelope({
            job_id: "00000000-0000-4000-8000-000000000012",
            status: "PENDING_DISPATCH",
          }))
        },
      ),
    )
    const gateway = createMarketDataGateway("http://localhost")

    await gateway.refreshSecurities("核对上市状态")
    await gateway.runQuoteOperation({
      action: "MANUAL_COLLECT",
      symbols: ["600000.SH"],
      reason: "补采监控股票",
      timeoutSeconds: 45,
    })
    await gateway.runQuoteOperation({
      action: "DIAGNOSE",
      symbols: ["600000.SH"],
      reason: "排查来源差异",
    })

    expect(received).toEqual([
      { confirm: true, reason: "核对上市状态" },
      {
        confirm: true,
        reason: "补采监控股票",
        symbols: ["600000.SH"],
        timeout_seconds: 45,
      },
      {
        confirm: true,
        reason: "排查来源差异",
        symbols: ["600000.SH"],
      },
    ])
  })

  it("前复权刷新提交当前数据集版本", async () => {
    server.use(
      http.post(
        "http://localhost/api/v1/qfq-data/:symbol/refresh",
        async ({ request, params }) => {
          expect(params.symbol).toBe("600519.SH")
          expect(request.headers.get("Idempotency-Key")).toMatch(/^web_/)
          expect(await request.json()).toEqual({
            start: "2010-01-01",
            end: "2026-07-22",
            as_of_date: "2026-07-22",
            confirm: true,
            reason: "刷新公司行为",
            expected_version: 4,
          })
          return HttpResponse.json(envelope({
            job_id: "00000000-0000-4000-8000-000000000013",
            job_type: "QFQ_REFRESH",
            status: "PENDING_DISPATCH",
          }))
        },
      ),
    )

    await expect(createMarketDataGateway("http://localhost").refreshQfq({
      dataset: {
        id: "00000000-0000-4000-8000-000000000014",
        symbol: "600519.SH",
        version: 4,
        actualStart: "2010-01-01",
        actualEnd: "2026-07-22",
        asOfDate: "2026-07-22",
        provider: "eastmoney",
        rowCount: 4000,
        lifecycle: "CURRENT",
        freshness: "FRESH",
        staleReason: null,
        activatedAt: "2026-07-22T09:00:00Z",
        allowedActions: ["REFRESH"],
      },
      reason: "刷新公司行为",
    })).resolves.toBeUndefined()
  })

  it("读取后端提供的质量候选来源和允许操作", async () => {
    server.use(
      http.get("http://localhost/api/v1/data-quality/issues", () => (
        HttpResponse.json(envelope({
          items: [issue],
          pagination: { page: 1, page_size: 50, total: 1 },
        }))
      )),
    )

    const result = await createMarketDataGateway("http://localhost")
      .loadQualityIssues()

    expect(result.items[0]).toEqual(expect.objectContaining({
      symbol: "600000.SH",
      sourceCandidates: ["tushare", "akshare"],
      allowedActions: ["SELECT_SOURCE", "INVALIDATE", "REFETCH"],
    }))
  })

  it("选择来源时提交确认、原因和服务端候选值，并携带幂等键", async () => {
    server.use(
      http.post(
        "http://localhost/api/v1/data-quality/issues/:issueId/select-source",
        async ({ request, params }) => {
          expect(params.issueId).toBe(issue.id)
          expect(request.headers.get("Idempotency-Key")).toMatch(/^web_/)
          expect(await request.json()).toEqual({
            confirm: true,
            reason: "交叉核对",
            selected_source: "akshare",
          })
          return HttpResponse.json(envelope({
            ...issue,
            status: "RESOLVED",
            selected_source: "akshare",
            allowed_actions: [],
          }))
        },
      ),
    )

    await expect(createMarketDataGateway("http://localhost").runQualityAction({
      issueId: issue.id,
      action: "SELECT_SOURCE",
      reason: "交叉核对",
      selectedSource: "akshare",
    })).resolves.toBeUndefined()
  })

  it("日线重试提交确认、原因和独立幂等键", async () => {
    server.use(
      http.post(
        "http://localhost/api/v1/daily-data/batches/:batchId/retry",
        async ({ request, params }) => {
          expect(params.batchId).toBe(
            "00000000-0000-4000-8000-000000000002",
          )
          expect(request.headers.get("Idempotency-Key")).toMatch(/^web_/)
          expect(await request.json()).toEqual({
            confirm: true,
            reason: "仅重试缺失股票",
          })
          return HttpResponse.json(envelope({
            job_id: "00000000-0000-4000-8000-000000000003",
            job_type: "DAILY_DATA_RETRY",
            status: "PENDING_DISPATCH",
          }))
        },
      ),
    )

    await expect(createMarketDataGateway("http://localhost").retryDailyBatch({
      batchId: "00000000-0000-4000-8000-000000000002",
      reason: "仅重试缺失股票",
    })).resolves.toBeUndefined()
  })

  it("历史回填控制提交页面读取到的任务版本", async () => {
    server.use(
      http.post(
        "http://localhost/api/v1/market-history/backfills/:jobId/pause",
        async ({ request }) => {
          expect(request.headers.get("Idempotency-Key")).toMatch(/^web_/)
          expect(await request.json()).toEqual({
            confirm: true,
            reason: "释放当日日线资源",
            expected_version: 7,
          })
          return HttpResponse.json(envelope({
            job_id: "00000000-0000-4000-8000-000000000004",
            status: "PAUSING",
            progress: null,
            result_summary: null,
            version: 8,
            created_at: "2026-07-23T03:00:00Z",
            updated_at: "2026-07-23T03:01:00Z",
            terminal_at: null,
          }))
        },
      ),
    )

    await expect(createMarketDataGateway("http://localhost").runBackfillAction({
      job: {
        id: "00000000-0000-4000-8000-000000000004",
        status: "RUNNING",
        version: 7,
        completed: 10,
        total: 100,
        succeeded: null,
        failed: null,
        updatedAt: "2026-07-23T03:00:00Z",
        terminalAt: null,
        allowedActions: ["PAUSE"],
      },
      action: "PAUSE",
      reason: "释放当日日线资源",
    })).resolves.toBeUndefined()
  })
})
