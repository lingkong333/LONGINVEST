import { http, HttpResponse } from "msw"
import { setupServer } from "msw/node"
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest"

import { createTargetManagementApi } from "@/features/targets"
import { createApiClient, type ApiEnvelope } from "@/shared/api/client"
import type { paths } from "@/shared/api/generated/schema"

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
    request_id: "req-target",
    server_time: "2026-07-23T03:00:00Z",
  }
}

const revision = {
  id: "revision-1",
  subscription_id: "subscription-1",
  revision_no: 2,
  values: {
    low_strong: "8.00",
    low_watch: "9.00",
    high_watch: "12.00",
    high_strong: "13.00",
  },
  source: "STRATEGY",
  source_revision_id: null,
  target_date: "2026-07-23",
  strategy_version_id: "strategy-1",
  parameter_snapshot: {},
  data_version: 8,
  source_code_hash: "a".repeat(64),
  content_hash: "b".repeat(64),
  reason: "策略计算",
  created_at: "2026-07-23T02:00:00Z",
}

function gateway() {
  return createTargetManagementApi(createApiClient<paths>({
    baseUrl: "http://localhost",
  }))
}

describe("目标价请求边界", () => {
  it("只使用后端返回的目标与复核允许操作和版本", async () => {
    server.use(
      http.get("http://localhost/api/v1/targets", () => (
        HttpResponse.json(envelope({
          items: [{
            ...revision,
            revision_id: revision.id,
            binding_version: 3,
            status: "READY",
            activated_at: "2026-07-23T02:00:00Z",
            allowed_actions: ["MANUAL_EDIT", "CALCULATE", "RESTORE"],
          }],
          pagination: { page: 1, page_size: 200, total: 1 },
        }))
      )),
      http.get("http://localhost/api/v1/target-reviews", () => (
        HttpResponse.json(envelope({
          items: [{
            id: "review-1",
            candidate_revision_id: revision.id,
            baseline_revision_id: revision.id,
            status: "PENDING",
            reason: "变化较大",
            low_strong_change: "0.1",
            low_watch_change: "0.1",
            high_watch_change: "0.1",
            high_strong_change: "0.1",
            reviewer_user_id: null,
            review_comment: null,
            reviewed_at: null,
            created_at: "2026-07-23T02:00:00Z",
            subscription_id: "subscription-1",
            binding_version: 3,
            candidate: revision,
            baseline: revision,
            allowed_actions: ["APPROVE", "REJECT", "RECALCULATE"],
          }],
          pagination: { page: 1, page_size: 200, total: 1 },
        }))
      )),
    )

    const targets = await gateway().listTargets()
    const reviews = await gateway().listReviews()

    expect(targets[0].allowedActions).toEqual([
      "MANUAL_EDIT",
      "CALCULATE",
      "RESTORE",
    ])
    expect(reviews[0]).toMatchObject({
      version: 3,
      allowedActions: ["APPROVE", "REJECT", "RECALCULATE"],
      candidate: { id: "revision-1" },
      baseline: { id: "revision-1" },
    })
  })

  it("手工目标提交当前版本、确认信息和幂等键", async () => {
    let body: unknown
    let idempotencyKey: string | null = null
    server.use(
      http.post("http://localhost/api/v1/targets/subscription-1/manual", async ({ request }) => {
        body = await request.json()
        idempotencyKey = request.headers.get("Idempotency-Key")
        return HttpResponse.json(envelope({ code: "TARGET_MANUAL_ACTIVATED" }))
      }),
    )

    await gateway().setManual("subscription-1", {
      targetDate: "2026-08-01",
      values: revision.values,
      reason: "人工确认",
      expectedVersion: 3,
      largeChangeConfirmed: false,
      switchToManualConfirmed: true,
    })

    expect(body).toMatchObject({
      confirm: true,
      reason: "人工确认",
      expected_version: 3,
      switch_to_manual_confirmed: true,
    })
    expect(idempotencyKey).toBeTruthy()
  })
})
