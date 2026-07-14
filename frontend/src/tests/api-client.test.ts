import { delay, http, HttpResponse } from "msw"
import { setupServer } from "msw/node"
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest"

import { createApiClient, unwrapEnvelope, type ApiEnvelope } from "@/shared/api/client"

interface TestPaths {
  "/status": {
    get: {
      responses: {
        200: { content: { "application/json": ApiEnvelope<{ ready: boolean }> } }
        401: { content: { "application/json": ApiEnvelope<null> } }
      }
    }
  }
  "/settings": {
    post: {
      requestBody: { content: { "application/json": { enabled: boolean } } }
      responses: {
        200: { content: { "application/json": ApiEnvelope<{ enabled: boolean }> } }
      }
    }
  }
}

const server = setupServer()

beforeAll(() => server.listen({ onUnhandledRequest: "error" }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

describe("统一 API 客户端", () => {
  it("查询请求携带 Cookie 语义和请求标识并解析标准包络", async () => {
    server.use(
      http.get("http://localhost/api/v1/status", ({ request }) => {
        expect(request.credentials).toBe("include")
        expect(request.headers.get("X-Request-ID")).toMatch(/^web_/)
        return HttpResponse.json({
          success: true,
          code: "OK",
          message: "操作成功",
          data: { ready: true },
          request_id: "req_ready",
          server_time: "2026-07-14T00:00:00Z",
        } satisfies ApiEnvelope<{ ready: boolean }>)
      }),
    )
    const client = createApiClient<TestPaths>({ baseUrl: "http://localhost/api/v1" })

    const { data } = await client.GET("/status")

    expect(unwrapEnvelope(data)).toEqual({ ready: true })
  })

  it("写请求携带内存 CSRF 和幂等键", async () => {
    server.use(
      http.post("http://localhost/api/v1/settings", async ({ request }) => {
        expect(request.headers.get("X-CSRF-Token")).toBe("csrf-memory-only")
        expect(request.headers.get("Idempotency-Key")).toMatch(/^web_/)
        return HttpResponse.json({
          success: true,
          code: "OK",
          message: "操作成功",
          data: { enabled: true },
          request_id: "req_write",
          server_time: "2026-07-14T00:00:00Z",
        } satisfies ApiEnvelope<{ enabled: boolean }>)
      }),
    )
    const client = createApiClient<TestPaths>({
      baseUrl: "http://localhost/api/v1",
      getCsrfToken: () => "csrf-memory-only",
    })

    const { data } = await client.POST("/settings", { body: { enabled: true } })

    expect(unwrapEnvelope(data)).toEqual({ enabled: true })
  })

  it("请求超过截止时间后返回稳定超时错误", async () => {
    server.use(
      http.get("http://localhost/api/v1/status", async () => {
        await delay(100)
        return HttpResponse.json({})
      }),
    )
    const client = createApiClient<TestPaths>({ baseUrl: "http://localhost/api/v1", timeoutMs: 10 })

    await expect(client.GET("/status")).rejects.toMatchObject({ code: "REQUEST_TIMEOUT" })
  })

  it("并发 401 只触发一次退出流程", async () => {
    server.use(
      http.get("http://localhost/api/v1/status", () => HttpResponse.json({
        success: false,
        code: "AUTH_REQUIRED",
        message: "登录已失效",
        data: null,
        request_id: "req_401",
        server_time: "2026-07-14T00:00:00Z",
      }, { status: 401 })),
    )
    const onUnauthorized = vi.fn(async () => undefined)
    const client = createApiClient<TestPaths>({ baseUrl: "http://localhost/api/v1", onUnauthorized })

    await Promise.all([client.GET("/status"), client.GET("/status")])

    expect(onUnauthorized).toHaveBeenCalledOnce()
  })
})

describe("标准包络错误", () => {
  it("保留稳定错误码、请求标识和字段错误", () => {
    expect(() => unwrapEnvelope({
      success: false,
      code: "VALIDATION_FAILED",
      message: "输入有误",
      data: null,
      details: { fields: { symbol: "代码格式不正确" } },
      request_id: "req_invalid",
      server_time: "2026-07-14T00:00:00Z",
    })).toThrow(expect.objectContaining({
      code: "VALIDATION_FAILED",
      requestId: "req_invalid",
      fieldErrors: { symbol: "代码格式不正确" },
    }))
  })
})
