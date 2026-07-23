import { fetchEventSource } from "@microsoft/fetch-event-source"
import { describe, expect, it, vi } from "vitest"

import { connectResourceEventStream } from "@/shared/realtime/resource-event-stream"

vi.mock("@microsoft/fetch-event-source", async (importOriginal) => {
  const original = await importOriginal<typeof import("@microsoft/fetch-event-source")>()
  return {
    ...original,
    fetchEventSource: vi.fn(),
  }
})

const mockedFetchEventSource = vi.mocked(fetchEventSource)

describe("实时事件连接", () => {
  it("使用同源会话连接并转发有效事件", async () => {
    const onEvent = vi.fn()
    const onStateChange = vi.fn()
    mockedFetchEventSource.mockImplementationOnce(async (url, options) => {
      await options.onopen?.(new Response(null, {
        status: 200,
        headers: { "Content-Type": "text/event-stream; charset=utf-8" },
      }))
      options.onmessage?.({
        id: "7",
        event: "resource.changed",
        data: JSON.stringify({
          resource_type: "providers",
          resource_id: "tushare",
          version: 7,
          topic: "provider.recovered",
        }),
        retry: 0,
      })
      expect(url).toBe("/api/v1/events/stream")
      expect(options.credentials).toBe("include")
      expect(options.openWhenHidden).toBe(false)
    })

    await connectResourceEventStream({
      signal: new AbortController().signal,
      onEvent,
      onStateChange,
      onUnauthorized: vi.fn(),
    })

    expect(onStateChange).toHaveBeenCalledWith("connected")
    expect(onEvent).toHaveBeenCalledWith({
      resourceType: "providers",
      resourceId: "tushare",
      version: 7,
      topic: "provider.recovered",
    })
  })

  it("网络失败采用有上限的指数重连间隔", async () => {
    const delays: unknown[] = []
    mockedFetchEventSource.mockImplementationOnce(async (_url, options) => {
      delays.push(options.onerror?.(new TypeError("offline")))
      delays.push(options.onerror?.(new TypeError("offline")))
      delays.push(options.onerror?.(new TypeError("offline")))
    })

    await connectResourceEventStream({
      signal: new AbortController().signal,
      onEvent: vi.fn(),
      onStateChange: vi.fn(),
      onUnauthorized: vi.fn(),
    })

    expect(delays).toEqual([1_000, 2_000, 4_000])
  })

  it("会话失效时停止重连并通知认证层", async () => {
    const onUnauthorized = vi.fn()
    mockedFetchEventSource.mockImplementationOnce(async (_url, options) => {
      try {
        await options.onopen?.(new Response(null, { status: 401 }))
      } catch (error) {
        options.onerror?.(error)
      }
    })

    await connectResourceEventStream({
      signal: new AbortController().signal,
      onEvent: vi.fn(),
      onStateChange: vi.fn(),
      onUnauthorized,
    })

    expect(onUnauthorized).toHaveBeenCalledOnce()
  })

  it("恢复位置过期时清空游标重新建立一次连接", async () => {
    mockedFetchEventSource
      .mockImplementationOnce(async (_url, options) => {
        try {
          await options.onopen?.(new Response(null, { status: 409 }))
        } catch (error) {
          options.onerror?.(error)
        }
      })
      .mockResolvedValueOnce(undefined)

    await connectResourceEventStream({
      signal: new AbortController().signal,
      onEvent: vi.fn(),
      onStateChange: vi.fn(),
      onUnauthorized: vi.fn(),
    })

    expect(mockedFetchEventSource).toHaveBeenCalledTimes(2)
  })
})
