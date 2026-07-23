import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import {
  SystemStatusPage,
  type SystemStatusGateway,
} from "@/features/system-status"
import { ApiError } from "@/shared/api/client"

function gateway(overrides: Partial<SystemStatusGateway> = {}): SystemStatusGateway {
  return {
    loadOverall: vi.fn().mockResolvedValue({
      status: "DEGRADED",
      updatedAt: "2026-07-23T09:00:00Z",
      componentCount: 2,
      unhealthyCount: 1,
      allowedActions: [],
    }),
    loadComponents: vi.fn().mockResolvedValue([{
      name: "PostgreSQL",
      category: "数据库",
      status: "HEALTHY",
      critical: true,
      source: "database",
      updatedAt: "2026-07-23T09:00:00Z",
      message: "连接正常",
      details: [{ key: "延迟", value: "12", unit: "ms" }],
      allowedActions: [],
    }]),
    loadRuntime: vi.fn().mockResolvedValue({
      workers: [{
        workerId: "worker-1",
        queue: "realtime",
        status: "IDLE",
        currentJobId: null,
        heartbeatAt: "2026-07-23T09:00:00Z",
        processedJobs: 20,
        failedJobs: 1,
      }],
      queues: [{
        name: "realtime",
        status: "HEALTHY",
        depth: 2,
        activeWorkers: 1,
        oldestJobAt: null,
        updatedAt: "2026-07-23T09:00:00Z",
      }],
      allowedActions: [],
    }),
    loadScheduling: vi.fn().mockResolvedValue({
      scheduler: {
        status: "HEALTHY",
        scanIntervalSeconds: 10,
        lastScanAt: "2026-07-23T09:00:00Z",
        databaseTime: "2026-07-23T09:00:00Z",
        automaticSchedulingPaused: false,
        pauseReason: null,
        updatedAt: "2026-07-23T09:00:00Z",
      },
      clock: {
        status: "HEALTHY",
        applicationTime: "2026-07-23T09:00:00Z",
        databaseTime: "2026-07-23T09:00:00Z",
        maxSkewSeconds: 0.2,
        automaticSchedulingPaused: false,
        sources: [],
        updatedAt: "2026-07-23T09:00:00Z",
      },
      allowedActions: [],
    }),
    loadOccurrences: vi.fn().mockResolvedValue({
      items: [{
        occurrenceId: "00000000-0000-4000-8000-000000000001",
        occurrenceType: "REALTIME_QUOTE",
        definitionId: "morning",
        scheduledTradeDate: "2026-07-23",
        scheduledAt: "2026-07-23T01:30:00Z",
        status: "DISPATCHED",
        jobId: null,
        missedReason: null,
        createdAt: "2026-07-23T01:30:00Z",
        allowedActions: [],
      }],
      page: 1,
      pageSize: 20,
      total: 1,
      allowedActions: [],
    }),
    ...overrides,
  }
}

function renderPage(api: SystemStatusGateway) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(<QueryClientProvider client={queryClient}><SystemStatusPage gateway={api} /></QueryClientProvider>)
}

describe("运行状态页", () => {
  it("并列展示总体、组件、运行进程、调度时钟和近期记录", async () => {
    renderPage(gateway())

    expect(await screen.findByText("当前状态")).toBeInTheDocument()
    expect(await screen.findByText("PostgreSQL")).toBeInTheDocument()
    expect(await screen.findByText("worker-1")).toBeInTheDocument()
    expect((await screen.findAllByText("自动调度")).length).toBeGreaterThan(0)
    expect(await screen.findByText("实时行情")).toBeInTheDocument()
  })

  it("各区域空数据时给出独立说明", async () => {
    renderPage(gateway({
      loadComponents: vi.fn().mockResolvedValue([]),
      loadRuntime: vi.fn().mockResolvedValue({ workers: [], queues: [], allowedActions: [] }),
      loadOccurrences: vi.fn().mockResolvedValue({ items: [], page: 1, pageSize: 20, total: 0, allowedActions: [] }),
    }))

    expect(await screen.findByText("暂无组件状态")).toBeInTheDocument()
    expect(await screen.findByText("暂无 Worker 和队列状态")).toBeInTheDocument()
    expect(await screen.findByText("暂无近期调度记录")).toBeInTheDocument()
  })

  it("一个区域失败不遮住其他区域，并可单独重试", async () => {
    const loadRuntime = vi.fn()
      .mockRejectedValueOnce(new ApiError("队列暂时不可用", {
        code: "QUEUE_UNAVAILABLE",
        requestId: "req-queue",
      }))
      .mockResolvedValueOnce({ workers: [], queues: [], allowedActions: [] })
    renderPage(gateway({ loadRuntime }))

    expect(await screen.findByText(/队列暂时不可用/)).toBeInTheDocument()
    expect(screen.getByText("PostgreSQL")).toBeInTheDocument()
    expect(screen.getAllByText("自动调度").length).toBeGreaterThan(0)
    await userEvent.click(screen.getByRole("region", { name: "Worker 与队列" }).querySelector("button[aria-label='刷新 Worker 与队列']")!)
    expect(await screen.findByText("暂无 Worker 和队列状态")).toBeInTheDocument()
    expect(loadRuntime).toHaveBeenCalledTimes(2)
  })

  it("每个区域可独立刷新且不会触发其他请求", async () => {
    const api = gateway()
    renderPage(api)
    await screen.findByText("PostgreSQL")

    await userEvent.click(screen.getByRole("button", { name: "刷新系统组件" }))

    expect(api.loadComponents).toHaveBeenCalledTimes(2)
    expect(api.loadOverall).toHaveBeenCalledTimes(1)
    expect(api.loadRuntime).toHaveBeenCalledTimes(1)
    expect(api.loadScheduling).toHaveBeenCalledTimes(1)
    expect(api.loadOccurrences).toHaveBeenCalledTimes(1)
  })

  it("空许可契约下不展示任何运行控制动作", async () => {
    renderPage(gateway())
    await screen.findByText("PostgreSQL")

    expect(screen.queryByRole("button", { name: /暂停/ })).not.toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /重启/ })).not.toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /执行/ })).not.toBeInTheDocument()
    expect(screen.queryByText(/原始日志/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Shell/)).not.toBeInTheDocument()
  })
})
