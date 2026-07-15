import { delay, http, HttpResponse } from "msw"
import { setupServer } from "msw/node"
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest"

import { ApiError, createApiClient, unwrapEnvelope, type ApiEnvelope } from "@/shared/api/client"
import { createAppQueryClient } from "@/shared/query/query-client"

interface TestPaths {
  "/status": {
    get: {
      responses: {
        200: { content: { "application/json": ApiEnvelope<{ ready: boolean }> } }
        401: { content: { "application/json": ApiEnvelope<null> } }
        503: { content: { "application/json": ApiEnvelope<null> } }
      }
    }
  }
  "/settings": {
    post: {
      requestBody: { content: { "application/json": { enabled: boolean } } }
      responses: {
        200: { content: { "application/json": ApiEnvelope<{ enabled: boolean }> } }
        422: { content: { "application/json": ApiEnvelope<null> } }
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
    const api = createApiClient<TestPaths>({ baseUrl: "http://localhost/api/v1" })

    const data = await api.request(api.client.GET("/status"))

    expect(data).toEqual({ ready: true })
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
    const api = createApiClient<TestPaths>({
      baseUrl: "http://localhost/api/v1",
      getCsrfToken: () => "csrf-memory-only",
    })

    const data = await api.request(api.client.POST("/settings", { body: { enabled: true } }))

    expect(data).toEqual({ enabled: true })
  })

  it("请求超过截止时间后返回稳定超时错误", async () => {
    server.use(
      http.get("http://localhost/api/v1/status", async () => {
        await delay(100)
        return HttpResponse.json({})
      }),
    )
    const api = createApiClient<TestPaths>({ baseUrl: "http://localhost/api/v1", timeoutMs: 10 })

    await expect(api.client.GET("/status")).rejects.toMatchObject({ code: "REQUEST_TIMEOUT" })
  })

  it("同一认证代际的延迟 401 只处理一次，reset 后新 401 可再次触发", async () => {
    let responseCount = 0
    server.use(
      http.get("http://localhost/api/v1/status", async () => {
        responseCount += 1
        if (responseCount === 2) {
          await delay(50)
        }
        return HttpResponse.json({
          success: false,
          code: "AUTH_REQUIRED",
          message: "登录已失效",
          data: null,
          request_id: "req_401",
          server_time: "2026-07-14T00:00:00Z",
        }, { status: 401 })
      }),
    )
    const onUnauthorized = vi.fn(async () => undefined)
    const api = createApiClient<TestPaths>({ baseUrl: "http://localhost/api/v1", onUnauthorized })

    await Promise.all([api.client.GET("/status"), api.client.GET("/status")])

    expect(onUnauthorized).toHaveBeenCalledOnce()

    api.resetUnauthorized()
    await api.client.GET("/status")

    expect(onUnauthorized).toHaveBeenCalledTimes(2)
  })

  it("reset 后忽略旧代际迟到的 401，只处理新代际 401", async () => {
    const unauthorizedResponse = () => HttpResponse.json({
      success: false,
      code: "AUTH_REQUIRED",
      message: "登录已失效",
      data: null,
      request_id: "req_401_generation",
      server_time: "2026-07-15T00:00:00Z",
    } satisfies ApiEnvelope<null>, { status: 401 })
    let resolveOldRequest: ((response: Response) => void) | undefined
    const controlledFetch = vi
      .fn<typeof fetch>()
      .mockImplementationOnce(() => new Promise<Response>((resolve) => {
        resolveOldRequest = resolve
      }))
      .mockImplementation(async () => unauthorizedResponse())
    const onUnauthorized = vi.fn(async () => undefined)
    const api = createApiClient<TestPaths>({
      baseUrl: "http://localhost/api/v1",
      fetch: controlledFetch,
      onUnauthorized,
    })

    const oldRequest = api.client.GET("/status")
    await vi.waitFor(() => expect(controlledFetch).toHaveBeenCalledOnce())
    api.resetUnauthorized()
    resolveOldRequest?.(unauthorizedResponse())
    await oldRequest

    expect(onUnauthorized).not.toHaveBeenCalled()

    await api.client.GET("/status")

    expect(onUnauthorized).toHaveBeenCalledOnce()
  })

  it("把网络 TypeError 转换为保留原因的稳定 ApiError", async () => {
    const networkFailure = new TypeError("Failed to fetch")
    const api = createApiClient<TestPaths>({
      baseUrl: "http://localhost/api/v1",
      fetch: vi.fn().mockRejectedValue(networkFailure),
    })

    await expect(api.request(api.client.GET("/status"))).rejects.toMatchObject({
      code: "NETWORK_ERROR",
      cause: networkFailure,
    })
  })

  it("把 422 错误包络统一转换为不重试的 ApiError", async () => {
    server.use(
      http.post("http://localhost/api/v1/settings", () => HttpResponse.json({
        success: false,
        code: "VALIDATION_FAILED",
        message: "输入有误",
        data: null,
        details: { fields: { enabled: "状态值无效" } },
        request_id: "req_422",
        server_time: "2026-07-15T00:00:00Z",
      }, { status: 422 })),
    )
    const api = createApiClient<TestPaths>({ baseUrl: "http://localhost/api/v1" })

    await expect(api.request(api.client.POST("/settings", { body: { enabled: true } }))).rejects.toMatchObject({
      status: 422,
      code: "VALIDATION_FAILED",
      requestId: "req_422",
      fieldErrors: { enabled: "状态值无效" },
    })
  })

  it("把 503 错误包络统一转换为可重试的 ApiError", async () => {
    server.use(
      http.get("http://localhost/api/v1/status", () => HttpResponse.json({
        success: false,
        code: "DEPENDENCY_UNAVAILABLE",
        message: "依赖暂不可用",
        data: null,
        request_id: "req_503",
        server_time: "2026-07-15T00:00:00Z",
      }, { status: 503 })),
    )
    const api = createApiClient<TestPaths>({ baseUrl: "http://localhost/api/v1" })

    await expect(api.request(api.client.GET("/status"))).rejects.toMatchObject({
      status: 503,
      code: "DEPENDENCY_UNAVAILABLE",
      requestId: "req_503",
    })
  })
})

describe("Query 重试边界", () => {
  it("4xx 不重试、5xx 和请求超时最多重试一次", () => {
    const queryClient = createAppQueryClient()
    const retry = queryClient.getDefaultOptions().queries?.retry

    expect(retry).toBeTypeOf("function")
    if (typeof retry !== "function") {
      throw new Error("Query retry 配置缺失")
    }

    expect(retry(0, new ApiError("invalid", { status: 422, code: "VALIDATION_FAILED" }))).toBe(false)
    expect(retry(0, new ApiError("unavailable", { status: 503, code: "DEPENDENCY_UNAVAILABLE" }))).toBe(true)
    expect(retry(0, new ApiError("timeout", { code: "REQUEST_TIMEOUT" }))).toBe(true)
    expect(retry(0, new ApiError("network", { code: "NETWORK_ERROR" }))).toBe(true)
    expect(retry(1, new ApiError("unavailable", { status: 503, code: "DEPENDENCY_UNAVAILABLE" }))).toBe(false)
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
