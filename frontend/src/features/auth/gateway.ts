import { z } from "zod"

import type { paths } from "@/shared/api/generated/schema"
import { ApiError, createApiClient } from "@/shared/api/client"
import type { AuthGateway, AuthState, LoginInput } from "@/features/auth/types"

const authStateSchema = z.object({
  user: z.object({
    id: z.string().min(1),
    username: z.string().min(1),
    status: z.enum(["ACTIVE", "DISABLED"]),
  }),
  session: z.object({
    id: z.string().min(1),
    status: z.string().min(1),
    current: z.boolean(),
    created_at: z.string().min(1),
    last_request_at: z.string().min(1),
    last_user_activity_at: z.string().min(1),
    absolute_expires_at: z.string().min(1),
    ip_summary: z.string().nullable(),
    user_agent_summary: z.string().nullable(),
  }),
})

const csrfSchema = z.object({
  csrf_token: z.string().min(1),
})

interface AuthGatewayOptions {
  baseUrl?: string
  fetch?: typeof globalThis.fetch
}

function parseResponse<T>(schema: z.ZodType<T>, value: unknown): T {
  const parsed = schema.safeParse(value)
  if (!parsed.success) {
    throw new ApiError("服务器返回了无法识别的认证数据。", {
      code: "INVALID_AUTH_RESPONSE",
      cause: parsed.error,
    })
  }
  return parsed.data
}

export function createAuthGateway({
  baseUrl = "",
  fetch,
}: AuthGatewayOptions = {}): AuthGateway {
  let csrfToken: string | undefined
  let unauthorizedHandler: (() => void) | undefined

  const api = createApiClient<paths>({
    baseUrl,
    fetch,
    getCsrfToken: () => csrfToken,
    onUnauthorized: () => {
      csrfToken = undefined
      unauthorizedHandler?.()
    },
  })

  async function loadSession(): Promise<AuthState> {
    const [authData, csrfData] = await Promise.all([
      api.request<unknown>(api.client.GET("/api/v1/auth/me")),
      api.request<unknown>(api.client.GET("/api/v1/auth/csrf")),
    ])
    const authState = parseResponse(authStateSchema, authData)
    csrfToken = parseResponse(csrfSchema, csrfData).csrf_token
    return authState
  }

  return {
    loadSession,
    async login(input: LoginInput) {
      await api.request(api.client.POST("/api/v1/auth/login", { body: input }))
      api.resetUnauthorized()
      return loadSession()
    },
    async logout() {
      try {
        await api.request(api.client.POST("/api/v1/auth/logout"))
      } finally {
        csrfToken = undefined
      }
    },
    setUnauthorizedHandler(handler) {
      unauthorizedHandler = handler
    },
  }
}

export const authGateway = createAuthGateway()
