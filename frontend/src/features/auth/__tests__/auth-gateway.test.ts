import { http, HttpResponse } from "msw"
import { setupServer } from "msw/node"
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest"

import { createAuthGateway } from "@/features/auth"
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
    request_id: "req-auth",
    server_time: "2026-07-23T00:00:00Z",
  }
}

const me = {
  user: { id: "user-1", username: "admin", status: "ACTIVE" as const },
  session: {
    id: "session-1",
    status: "ACTIVE",
    current: true,
    created_at: "2026-07-23T00:00:00Z",
    last_request_at: "2026-07-23T00:00:00Z",
    last_user_activity_at: "2026-07-23T00:00:00Z",
    absolute_expires_at: "2026-10-23T00:00:00Z",
    ip_summary: "127.0.0.x",
    user_agent_summary: "test",
  },
}

describe("认证请求边界", () => {
  it("登录后并行恢复用户和 CSRF，并在退出时发送内存令牌", async () => {
    let logoutCsrf: string | null = null
    server.use(
      http.post("http://localhost/api/v1/auth/login", () => (
        HttpResponse.json(envelope({ session_id: "session-1" }))
      )),
      http.get("http://localhost/api/v1/auth/me", () => (
        HttpResponse.json(envelope(me))
      )),
      http.get("http://localhost/api/v1/auth/csrf", () => (
        HttpResponse.json(envelope({ csrf_token: "memory-csrf" }))
      )),
      http.post("http://localhost/api/v1/auth/logout", ({ request }) => {
        logoutCsrf = request.headers.get("X-CSRF-Token")
        return HttpResponse.json(envelope({ logged_out: true }))
      }),
    )
    const gateway = createAuthGateway({ baseUrl: "http://localhost" })

    await expect(gateway.login({
      username: "admin",
      password: "correct-password",
    })).resolves.toEqual(me)
    await gateway.logout()

    expect(logoutCsrf).toBe("memory-csrf")
  })

  it("同一批并发 401 只触发一次统一失效处理", async () => {
    server.use(
      http.get("http://localhost/api/v1/auth/me", () => (
        HttpResponse.json({
          ...envelope(null),
          success: false,
          code: "AUTH_REQUIRED",
        }, { status: 401 })
      )),
      http.get("http://localhost/api/v1/auth/csrf", () => (
        HttpResponse.json({
          ...envelope(null),
          success: false,
          code: "AUTH_REQUIRED",
        }, { status: 401 })
      )),
    )
    const gateway = createAuthGateway({ baseUrl: "http://localhost" })
    const onUnauthorized = vi.fn()
    gateway.setUnauthorizedHandler(onUnauthorized)

    await expect(gateway.loadSession()).rejects.toMatchObject({
      code: "AUTH_REQUIRED",
      status: 401,
    })

    expect(onUnauthorized).toHaveBeenCalledOnce()
  })
})
