import { http, HttpResponse } from "msw"
import { setupServer } from "msw/node"
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest"

import { createJobGateway } from "@/features/jobs/gateway"
import type { JobAction } from "@/features/jobs/types"
import type { ApiEnvelope } from "@/shared/api/client"

const server = setupServer()
const baseUrl = "http://localhost"
const jobId = "00000000-0000-4000-8000-000000000001"

beforeAll(() => server.listen({ onUnhandledRequest: "error" }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

function envelope<T>(data: T): ApiEnvelope<T> {
  return {
    success: true,
    code: "OK",
    message: "操作成功",
    data,
    request_id: "req-jobs",
    server_time: "2026-07-23T04:00:00Z",
  }
}

const job = {
  id: jobId,
  job_type: "BULK_HISTORY",
  business_object_type: "history_backfill",
  business_object_id: "backfill-1",
  queue: "bulk-history",
  priority: 50,
  status: "RUNNING",
  progress: { completed: 3, total: 10 },
  result_summary: null,
  current_run_id: "00000000-0000-4000-8000-000000000002",
  version: 4,
  created_at: "2026-07-23T03:00:00Z",
  updated_at: "2026-07-23T04:00:00Z",
  terminal_at: null,
}

describe("任务管理请求边界", () => {
  it("按筛选和分页读取任务列表", async () => {
    server.use(http.get(`${baseUrl}/api/v1/jobs`, ({ request }) => {
      const url = new URL(request.url)
      expect(url.searchParams.get("page")).toBe("2")
      expect(url.searchParams.get("page_size")).toBe("20")
      expect(url.searchParams.get("status")).toBe("RUNNING")
      expect(url.searchParams.get("job_type")).toBe("BULK_HISTORY")
      expect(url.searchParams.get("queue")).toBe("bulk-history")
      return HttpResponse.json(envelope({
        items: [job],
        pagination: { page: 2, page_size: 20, total: 24 },
      }))
    }))

    const result = await createJobGateway(baseUrl).loadJobs({
      page: 2,
      pageSize: 20,
      status: "RUNNING",
      jobType: "BULK_HISTORY",
      queue: "bulk-history",
    })

    expect(result.pagination).toEqual({ page: 2, pageSize: 20, total: 24 })
    expect(result.items[0]).toEqual(expect.objectContaining({
      id: jobId,
      status: "RUNNING",
      version: 4,
    }))
  })

  it("并行读取详情、运行尝试、逐项结果和允许操作", async () => {
    server.use(
      http.get(`${baseUrl}/api/v1/jobs/:jobId`, () => (
        HttpResponse.json(envelope({
          ...job,
          config_snapshot: { start: "2010-01-01" },
          request_id: "req-create",
          created_by_user_id: "user-1",
          soft_timeout_seconds: 300,
          hard_timeout_seconds: 600,
        }))
      )),
      http.get(`${baseUrl}/api/v1/jobs/:jobId/runs`, () => (
        HttpResponse.json(envelope({
          items: [{
            id: "00000000-0000-4000-8000-000000000002",
            job_id: jobId,
            attempt_no: 1,
            worker_id: "worker-1",
            status: "RUNNING",
            claimed_at: "2026-07-23T03:01:00Z",
            started_at: "2026-07-23T03:01:01Z",
            ended_at: null,
            heartbeat_at: "2026-07-23T04:00:00Z",
            exit_type: null,
            error_code: null,
            error_summary: null,
            metrics: { rows: 3 },
          }],
        }))
      )),
      http.get(`${baseUrl}/api/v1/jobs/:jobId/items`, () => (
        HttpResponse.json(envelope({
          items: [{
            id: "00000000-0000-4000-8000-000000000003",
            job_id: jobId,
            item_key: "600000.SH",
            status: "SUCCEEDED",
            attempt_count: 1,
            result_ref: "daily-bars:600000.SH",
            error_code: null,
            created_at: "2026-07-23T03:00:00Z",
            started_at: "2026-07-23T03:02:00Z",
            ended_at: "2026-07-23T03:03:00Z",
            updated_at: "2026-07-23T03:03:00Z",
          }],
          pagination: { page: 1, page_size: 100, total: 1 },
        }))
      )),
      http.get(`${baseUrl}/api/v1/jobs/:jobId/allowed-actions`, () => (
        HttpResponse.json(envelope({
          job_id: jobId,
          allowed_actions: ["pause", "cancel"],
        }))
      )),
    )

    const result = await createJobGateway(baseUrl).loadDetails(jobId)

    expect(result.job.requestId).toBe("req-create")
    expect(result.runs[0].workerId).toBe("worker-1")
    expect(result.items[0].itemKey).toBe("600000.SH")
    expect(result.allowedActions).toEqual(["pause", "cancel"])
  })

  it.each<JobAction>([
    "pause",
    "resume",
    "cancel",
    "retry",
    "retry-failed-items",
  ])("提交 %s 时携带确认、原因、版本和幂等标识", async (action) => {
    server.use(http.post(
      `${baseUrl}/api/v1/jobs/:jobId/${action}`,
      async ({ request, params }) => {
        expect(params.jobId).toBe(jobId)
        expect(request.headers.get("Idempotency-Key")).toMatch(/^web_/)
        expect(await request.json()).toEqual({
          confirm: true,
          reason: "人工确认任务状态",
          expected_version: 4,
        })
        return HttpResponse.json(envelope({
          job_id: jobId,
          status: "PAUSING",
          version: 5,
          allowed_actions: ["cancel"],
        }))
      },
    ))

    await expect(createJobGateway(baseUrl).runAction({
      jobId,
      action,
      reason: "人工确认任务状态",
      expectedVersion: 4,
    })).resolves.toBeUndefined()
  })
})
