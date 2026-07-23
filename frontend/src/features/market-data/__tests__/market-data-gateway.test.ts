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
})
