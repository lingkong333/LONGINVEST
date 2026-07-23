import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import {
  ProvidersPage,
  type ProviderGateway,
  type ProviderSummary,
} from "@/features/providers"
import { ApiError } from "@/shared/api/client"

const provider: ProviderSummary = {
  code: "EASTMONEY",
  version: 3,
  reason: "调整限流",
  capabilities: [{
    capability: "REALTIME_QUOTE_BATCH",
    enabled: true,
    priority: 1,
    concurrency: 2,
    ratePerSecond: 3,
    timeoutSeconds: 5,
    autoSwitch: true,
  }],
  allowedActions: ["UPDATE_SETTINGS", "QUOTE_DIAGNOSTICS"],
}

function gateway(overrides: Partial<ProviderGateway> = {}): ProviderGateway {
  return {
    loadProviders: vi.fn().mockResolvedValue([provider]),
    loadHealth: vi.fn().mockResolvedValue([{
      capability: "REALTIME_QUOTE_BATCH",
      status: "HEALTHY",
      consecutiveFailures: 0,
      lastSuccessAt: "2026-07-23T08:00:00Z",
      lastFailureAt: null,
      successRate: 0.99,
      p95LatencyMs: 180,
      rateLimitWaitMs: 3,
      switchCount: 0,
      schemaErrors: 0,
    }]),
    loadCircuits: vi.fn().mockResolvedValue([{
      id: "00000000-0000-4000-8000-000000000001",
      providerCode: "EASTMONEY",
      capability: "REALTIME_QUOTE_BATCH",
      state: "OPEN",
      consecutiveFailures: 3,
      cooldownIndex: 1,
      openedAt: "2026-07-23T07:00:00Z",
      allowedActions: ["PROBE", "RESET"],
    }]),
    updateSettings: vi.fn().mockResolvedValue(undefined),
    runCircuitAction: vi.fn().mockResolvedValue(undefined),
    runQuoteDiagnostics: vi.fn().mockResolvedValue({
      symbols: ["600000.SH"],
      sources: [],
      comparisons: [{
        symbol: "600000.SH",
        status: "INCOMPLETE",
        missingSources: ["EASTMONEY", "SINA"],
      }],
    }),
    ...overrides,
  }
}

function renderPage(api: ProviderGateway) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(<QueryClientProvider client={queryClient}><ProvidersPage gateway={api} /></QueryClientProvider>)
}

describe("数据源管理页", () => {
  it("加载后展示能力、健康和熔断状态，且没有危险网络输入", async () => {
    renderPage(gateway())

    expect((await screen.findAllByText("东方财富")).length).toBeGreaterThan(0)
    expect(screen.getAllByText("批量实时行情").length).toBeGreaterThan(0)
    expect(await screen.findByText("健康")).toBeInTheDocument()
    expect(await screen.findByText("已熔断")).toBeInTheDocument()
    for (const label of ["URL", "代理", "Header", "Cookie", "脚本"]) {
      expect(screen.queryByLabelText(label)).not.toBeInTheDocument()
    }
  })

  it("加载、空数据和接口失败都有明确页面状态", async () => {
    let resolve: ((value: ProviderSummary[]) => void) | undefined
    const pending = new Promise<ProviderSummary[]>((done) => { resolve = done })
    const { unmount } = renderPageWithResult(gateway({ loadProviders: vi.fn(() => pending) }))
    expect(screen.getByText("正在读取数据源")).toBeInTheDocument()
    resolve?.([])
    expect(await screen.findByText("暂无已注册数据源")).toBeInTheDocument()
    unmount()

    renderPage(gateway({
      loadProviders: vi.fn().mockRejectedValue(new ApiError("服务不可用", {
        code: "PROVIDER_UNAVAILABLE",
        requestId: "req-failed",
      })),
    }))
    expect(await screen.findByText("数据源暂时无法读取")).toBeInTheDocument()
    expect(screen.getByText("PROVIDER_UNAVAILABLE")).toBeInTheDocument()
  })

  it("后端未许可时所有写操作保持禁用", async () => {
    renderPage(gateway({
      loadProviders: vi.fn().mockResolvedValue([{
        ...provider,
        allowedActions: [],
      }]),
      loadCircuits: vi.fn().mockResolvedValue([{
        ...(await gateway().loadCircuits())[0],
        allowedActions: [],
      }]),
    }))

    expect(await screen.findByRole("button", { name: "编辑 东方财富 配置" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "行情诊断" })).toBeDisabled()
    expect(await screen.findByRole("button", { name: /探测 东方财富/ })).toBeDisabled()
    expect(screen.getByRole("button", { name: /重置 东方财富/ })).toBeDisabled()
  })

  it("配置操作要求原因、防重复，并在成功后刷新", async () => {
    let finish: (() => void) | undefined
    const updateSettings = vi.fn(() => new Promise<void>((resolve) => { finish = resolve }))
    const loadProviders = vi.fn().mockResolvedValue([provider])
    renderPage(gateway({ updateSettings, loadProviders }))

    await userEvent.click(await screen.findByRole("button", { name: "编辑 东方财富 配置" }))
    const dialog = screen.getByRole("dialog")
    const submit = within(dialog).getByRole("button", { name: "确认执行" })
    expect(submit).toBeDisabled()
    await userEvent.clear(within(dialog).getByLabelText("并发上限"))
    await userEvent.type(within(dialog).getByLabelText("并发上限"), "4")
    await userEvent.type(within(dialog).getByLabelText("操作原因"), "控制上游压力")
    await userEvent.click(submit)
    await userEvent.click(submit)

    expect(updateSettings).toHaveBeenCalledTimes(1)
    expect(updateSettings).toHaveBeenCalledWith(expect.objectContaining({
      provider,
      settings: expect.objectContaining({ concurrency: 4 }),
      reason: "控制上游压力",
    }))
    expect(within(dialog).getByRole("button", { name: "正在提交" })).toBeDisabled()
    finish?.()
    expect(await screen.findByText("数据源管理")).toBeInTheDocument()
    expect(loadProviders).toHaveBeenCalledTimes(2)
  })

  it("操作失败时保留原因和表单，允许用户修正后重试", async () => {
    const updateSettings = vi.fn().mockRejectedValue(new Error("配置版本已变化"))
    renderPage(gateway({ updateSettings }))

    await userEvent.click(await screen.findByRole("button", { name: "编辑 东方财富 配置" }))
    const dialog = screen.getByRole("dialog")
    await userEvent.type(within(dialog).getByLabelText("操作原因"), "调整超时")
    await userEvent.click(within(dialog).getByRole("button", { name: "确认执行" }))

    expect(await within(dialog).findByText("配置版本已变化")).toBeInTheDocument()
    expect(within(dialog).getByLabelText("操作原因")).toHaveValue("调整超时")
    expect(within(dialog).getByLabelText("超时（秒）")).toHaveValue(5)
  })

  it("行情诊断仅提交股票代码和原因，并显示比较结果", async () => {
    const runQuoteDiagnostics = vi.fn().mockResolvedValue({
      symbols: ["600000.SH"],
      sources: [],
      comparisons: [{
        symbol: "600000.SH",
        status: "CONFLICT" as const,
        missingSources: [],
      }],
    })
    renderPage(gateway({ runQuoteDiagnostics }))

    await userEvent.click(await screen.findByRole("button", { name: "行情诊断" }))
    const dialog = screen.getByRole("dialog")
    await userEvent.type(within(dialog).getByLabelText("股票代码"), "600000.sh")
    await userEvent.type(within(dialog).getByLabelText("操作原因"), "核对报价差异")
    await userEvent.click(within(dialog).getByRole("button", { name: "确认诊断" }))

    expect(runQuoteDiagnostics).toHaveBeenCalledWith(["600000.SH"], "核对报价差异")
    expect(await within(dialog).findByText("600000.SH：存在差异")).toBeInTheDocument()
  })
})

function renderPageWithResult(api: ProviderGateway) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(<QueryClientProvider client={queryClient}><ProvidersPage gateway={api} /></QueryClientProvider>)
}
