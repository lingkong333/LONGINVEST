import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, render, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import {
  AuthContext,
  type AuthContextValue,
  type AuthPhase,
} from "@/features/auth/auth-context"
import { ResourceEventProvider } from "@/app/resource-event-provider"
import type {
  ResourceEventConnectionOptions,
  ResourceEventConnector,
} from "@/shared/realtime/resource-event-stream"
import { parseResourceChangedEvent } from "@/shared/realtime/resource-events"

function authValue(phase: AuthPhase, invalidate = vi.fn()): AuthContextValue {
  return {
    phase,
    auth: null,
    error: null,
    isSubmitting: false,
    invalidate,
    login: vi.fn(),
    logout: vi.fn(),
    retry: vi.fn(),
  }
}

function pendingConnector() {
  let options: ResourceEventConnectionOptions | undefined
  const connector: ResourceEventConnector = vi.fn((nextOptions) => {
    options = nextOptions
    return new Promise<void>((resolve) => {
      nextOptions.signal.addEventListener("abort", () => resolve(), { once: true })
    })
  })
  return {
    connector,
    options: () => {
      if (!options) {
        throw new Error("connector was not started")
      }
      return options
    },
  }
}

function renderProvider(
  phase: AuthPhase,
  connector: ResourceEventConnector,
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  }),
  invalidate = vi.fn(),
) {
  const view = render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={authValue(phase, invalidate)}>
        <ResourceEventProvider connector={connector}>
          <div>content</div>
        </ResourceEventProvider>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
  return { ...view, invalidate, queryClient }
}

afterEach(() => {
  vi.useRealTimers()
})

describe("共享实时事件", () => {
  it("只在登录后建立一条连接，并在卸载时关闭", () => {
    const stream = pendingConnector()
    const view = renderProvider("unauthenticated", stream.connector)

    expect(stream.connector).not.toHaveBeenCalled()

    view.rerender(
      <QueryClientProvider client={view.queryClient}>
        <AuthContext.Provider value={authValue("authenticated", view.invalidate)}>
          <ResourceEventProvider connector={stream.connector}>
            <div>content</div>
          </ResourceEventProvider>
        </AuthContext.Provider>
      </QueryClientProvider>,
    )

    expect(stream.connector).toHaveBeenCalledOnce()
    const signal = stream.options().signal
    view.unmount()
    expect(signal.aborted).toBe(true)
  })

  it("按资源刷新页面，忽略重复和乱序事件", async () => {
    const stream = pendingConnector()
    const queryClient = new QueryClient()
    const invalidate = vi.spyOn(queryClient, "invalidateQueries")
    renderProvider("authenticated", stream.connector, queryClient)

    act(() => {
      stream.options().onEvent({
        resourceType: "jobs",
        resourceId: "job-42",
        version: 42,
        topic: "job.changed.v1",
      })
    })

    await waitFor(() => expect(invalidate).toHaveBeenCalledTimes(4))
    expect(invalidate.mock.calls.map(([options]) => options?.queryKey)).toEqual([
      ["jobs"],
      ["dashboard"],
      ["monitoring"],
      ["system-status"],
    ])

    act(() => {
      stream.options().onEvent({
        resourceType: "alerts",
        resourceId: "alert-old",
        version: 41,
        topic: "alert.updated.v1",
      })
    })
    expect(invalidate).toHaveBeenCalledTimes(4)
  })

  it("断线时轮询当前页面数据，恢复连接后停止轮询", async () => {
    vi.useFakeTimers()
    const stream = pendingConnector()
    const queryClient = new QueryClient()
    const invalidate = vi.spyOn(queryClient, "invalidateQueries")
    renderProvider("authenticated", stream.connector, queryClient)

    act(() => {
      stream.options().onStateChange("reconnecting")
      vi.advanceTimersByTime(30_000)
    })
    await vi.waitFor(() => expect(invalidate).toHaveBeenCalledTimes(10))

    act(() => {
      stream.options().onStateChange("connected")
      vi.advanceTimersByTime(120_000)
    })
    expect(invalidate).toHaveBeenCalledTimes(10)
  })

  it("服务端拒绝会话时统一退出登录", () => {
    const stream = pendingConnector()
    const invalidateSession = vi.fn()
    renderProvider("authenticated", stream.connector, undefined, invalidateSession)

    act(() => stream.options().onUnauthorized())

    expect(invalidateSession).toHaveBeenCalledOnce()
  })
})

describe("实时事件解析", () => {
  it("接受完整的资源变化消息", () => {
    expect(parseResourceChangedEvent(JSON.stringify({
      resource_type: "signals",
      resource_id: "subscription-1",
      version: 9,
      topic: "signal.transitioned",
    }))).toEqual({
      resourceType: "signals",
      resourceId: "subscription-1",
      version: 9,
      topic: "signal.transitioned",
    })
  })

  it.each([
    "not-json",
    "{}",
    JSON.stringify({
      resource_type: "unknown",
      resource_id: "id",
      version: 1,
      topic: "unknown.changed",
    }),
    JSON.stringify({
      resource_type: "jobs",
      resource_id: "",
      version: 1,
      topic: "job.changed.v1",
    }),
  ])("隔离无法识别的消息", (data) => {
    expect(parseResourceChangedEvent(data)).toBeNull()
  })
})
