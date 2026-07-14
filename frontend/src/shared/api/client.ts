import createClient from "openapi-fetch"

export interface ApiEnvelope<T> {
  success: boolean
  code: string
  message: string
  data: T | null
  details?: {
    fields?: Record<string, string>
    current_version?: string | number
    allowed_actions?: string[]
  }
  request_id: string
  server_time: string
}

export class ApiError extends Error {
  readonly code: string
  readonly requestId?: string
  readonly fieldErrors?: Record<string, string>
  readonly status?: number

  constructor(
    message: string,
    options: {
      code: string
      requestId?: string
      fieldErrors?: Record<string, string>
      status?: number
      cause?: unknown
    },
  ) {
    super(message, { cause: options.cause })
    this.name = "ApiError"
    this.code = options.code
    this.requestId = options.requestId
    this.fieldErrors = options.fieldErrors
    this.status = options.status
  }
}

interface ApiClientOptions {
  baseUrl?: string
  timeoutMs?: number
  getCsrfToken?: () => string | undefined
  onUnauthorized?: () => void | Promise<void>
  fetch?: typeof globalThis.fetch
}

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"])

interface ApiOperationResult<T> {
  data?: ApiEnvelope<T>
  error?: unknown
  response: Response
}

export function createClientRequestId() {
  return `web_${globalThis.crypto.randomUUID()}`
}

function isApiEnvelope(value: unknown): value is ApiEnvelope<unknown> {
  if (!value || typeof value !== "object") {
    return false
  }
  const candidate = value as Partial<ApiEnvelope<unknown>>
  return (
    typeof candidate.success === "boolean"
    && typeof candidate.code === "string"
    && typeof candidate.message === "string"
    && typeof candidate.request_id === "string"
  )
}

function apiErrorFromEnvelope(envelope: ApiEnvelope<unknown>, status?: number) {
  return new ApiError(envelope.message, {
    code: envelope.code,
    requestId: envelope.request_id,
    fieldErrors: envelope.details?.fields,
    status,
  })
}

export function unwrapEnvelope<T>(envelope: ApiEnvelope<T> | undefined, status?: number): T {
  if (!envelope) {
    throw new ApiError("服务器返回了无法识别的响应。", {
      code: "INVALID_RESPONSE",
      status,
    })
  }

  if (!envelope.success || envelope.data === null) {
    throw apiErrorFromEnvelope(envelope, status)
  }

  return envelope.data
}

export function createApiClient<Paths extends object>({
  baseUrl = "/api/v1",
  timeoutMs = 15_000,
  getCsrfToken,
  onUnauthorized,
  fetch: fetchImplementation = globalThis.fetch,
}: ApiClientOptions = {}) {
  let unauthorizedWave: Promise<void> | undefined

  const handleUnauthorized = () => {
    if (!onUnauthorized) {
      return Promise.resolve()
    }
    if (!unauthorizedWave) {
      unauthorizedWave = Promise.resolve()
        .then(onUnauthorized)
        .finally(() => {
          unauthorizedWave = undefined
        })
    }
    return unauthorizedWave
  }

  const guardedFetch: typeof globalThis.fetch = async (input, init) => {
    const originalRequest = input instanceof Request ? input : new Request(input, init)
    const headers = new Headers(originalRequest.headers)
    const method = originalRequest.method.toUpperCase()
    const isWrite = !SAFE_METHODS.has(method)

    if (!headers.has("X-Request-ID")) {
      headers.set("X-Request-ID", createClientRequestId())
    }
    if (isWrite && !headers.has("Idempotency-Key")) {
      headers.set("Idempotency-Key", createClientRequestId())
    }
    const csrfToken = isWrite ? getCsrfToken?.() : undefined
    if (csrfToken && !headers.has("X-CSRF-Token")) {
      headers.set("X-CSRF-Token", csrfToken)
    }

    const controller = new AbortController()
    const sourceSignal = originalRequest.signal
    const relayAbort = () => controller.abort(sourceSignal.reason)
    sourceSignal.addEventListener("abort", relayAbort, { once: true })
    const timeout = globalThis.setTimeout(() => controller.abort("timeout"), timeoutMs)

    try {
      const request = new Request(originalRequest, {
        credentials: "include",
        headers,
        signal: controller.signal,
      })
      const response = await fetchImplementation(request)
      if (response.status === 401) {
        await handleUnauthorized()
      }
      return response
    } catch (error) {
      if (controller.signal.aborted && !sourceSignal.aborted) {
        throw new ApiError("请求超时，请稍后重试。", {
          code: "REQUEST_TIMEOUT",
          cause: error,
        })
      }
      throw error
    } finally {
      globalThis.clearTimeout(timeout)
      sourceSignal.removeEventListener("abort", relayAbort)
    }
  }

  const client = createClient<Paths>({ baseUrl, fetch: guardedFetch })

  async function request<T>(operation: Promise<ApiOperationResult<T>>) {
    const result = await operation
    if (!result.response.ok || result.error !== undefined) {
      if (isApiEnvelope(result.error)) {
        throw apiErrorFromEnvelope(result.error, result.response.status)
      }
      throw new ApiError("服务器请求失败。", {
        code: `HTTP_${result.response.status}`,
        requestId: result.response.headers.get("X-Request-ID") ?? undefined,
        status: result.response.status,
      })
    }
    return unwrapEnvelope(result.data, result.response.status)
  }

  return { client, request }
}
