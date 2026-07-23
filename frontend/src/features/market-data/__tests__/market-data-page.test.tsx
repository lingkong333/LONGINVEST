import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import {
  MarketDataPage,
  type MarketDataGateway,
} from "@/features/market-data"
import { ApiError } from "@/shared/api/client"

function page<Item>(items: Item[]) {
  return {
    items,
    pagination: { page: 1, pageSize: 50, total: items.length },
  }
}

function gateway(overrides: Partial<MarketDataGateway> = {}): MarketDataGateway {
  return {
    loadSecurities: vi.fn().mockResolvedValue({
      ...page([{
      id: "600519.SH",
      symbol: "600519.SH",
      name: "贵州茅台",
      market: "主板",
      listingStatus: "LISTED",
      isSt: false,
      isSuspended: false,
      masterVersion: 12,
      updatedAt: "2026-07-23T02:00:00Z",
      }]),
      allowedActions: ["REFRESH"],
    }),
    refreshSecurities: vi.fn().mockResolvedValue(undefined),
    loadQuoteCycles: vi.fn().mockResolvedValue({
      ...page([{
      id: "cycle-1",
      status: "PARTIAL",
      expectedCount: 2,
      validCount: 1,
      missingCount: 1,
      conflictCount: 0,
      failedCount: 0,
      scheduledAt: "2026-07-23T02:00:00Z",
      finalizedAt: "2026-07-23T02:00:30Z",
      }]),
      allowedActions: ["MANUAL_COLLECT", "DIAGNOSE"],
    }),
    loadQuoteItems: vi.fn().mockResolvedValue([{
      id: "item-1",
      symbol: "600519.SH",
      status: "VALID",
      price: "1688.00",
      provider: "tushare",
      quoteTime: "2026-07-23T02:00:10Z",
      errorCode: null,
      eligibleForEvaluation: true,
    }]),
    runQuoteOperation: vi.fn().mockResolvedValue(undefined),
    loadDailyBatches: vi.fn().mockResolvedValue(page([{
      id: "batch-1",
      tradingDate: "2026-07-22",
      status: "COMPLETED",
      expectedCount: 5000,
      fetchedCount: 4999,
      committedCount: 4999,
      missingCount: 1,
      failedCount: 0,
      createdAt: "2026-07-22T09:00:00Z",
      completedAt: "2026-07-22T09:20:00Z",
      allowedActions: ["RETRY_MISSING"],
    }])),
    retryDailyBatch: vi.fn().mockResolvedValue(undefined),
    loadQfq: vi.fn().mockResolvedValue({
      id: "qfq-1",
      symbol: "600519.SH",
      version: 3,
      actualStart: "2010-01-01",
      actualEnd: "2026-07-22",
      asOfDate: "2026-07-22",
      provider: "tushare",
      rowCount: 3999,
      lifecycle: "CURRENT",
      freshness: "FRESH",
      staleReason: null,
      activatedAt: "2026-07-23T02:00:00Z",
      allowedActions: ["REFRESH"],
    }),
    refreshQfq: vi.fn().mockResolvedValue(undefined),
    loadQualityIssues: vi.fn().mockResolvedValue(page([{
      id: "issue-1",
      issueType: "SOURCE_CONFLICT",
      subjectType: "QUOTE",
      symbol: "600000.SH",
      status: "OPEN",
      severity: "WARNING",
      occurrenceCount: 2,
      lastSeenAt: "2026-07-23T02:00:00Z",
      selectedSource: null,
      sourceCandidates: ["tushare", "akshare"],
      allowedActions: ["SELECT_SOURCE", "INVALIDATE", "REFETCH"],
    }])),
    runQualityAction: vi.fn().mockResolvedValue(undefined),
    loadBackfills: vi.fn().mockResolvedValue({
      ...page([{
      id: "backfill-1",
      status: "RUNNING",
      version: 2,
      completed: 80,
      total: 100,
      succeeded: null,
      failed: null,
      updatedAt: "2026-07-23T02:00:00Z",
      terminalAt: null,
      allowedActions: ["PAUSE", "CANCEL"],
      }]),
      allowedActions: ["CREATE"],
    }),
    createBackfill: vi.fn().mockResolvedValue(undefined),
    runBackfillAction: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  }
}

function renderPage(marketGateway: MarketDataGateway) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
  render(
    <QueryClientProvider client={queryClient}>
      <MarketDataPage gateway={marketGateway} />
    </QueryClientProvider>,
  )
}

describe("行情数据中心", () => {
  it("并列展示六类数据且不提供人工价格输入", async () => {
    renderPage(gateway())

    expect(await screen.findByText("贵州茅台")).toBeInTheDocument()
    expect(screen.getByText("实时采集周期")).toBeInTheDocument()
    expect(screen.getByText("日线批次")).toBeInTheDocument()
    expect(screen.getByText("前复权数据")).toBeInTheDocument()
    expect(screen.getByText(/来源冲突/)).toBeInTheDocument()
    expect(screen.getByText("历史回填")).toBeInTheDocument()
    expect(screen.getByText("价格仅由数据源采集，不支持人工录入")).toBeInTheDocument()
    expect(screen.queryByRole("textbox", { name: "价格" })).not.toBeInTheDocument()
  })

  it("单个区域失败不会遮住其他区域", async () => {
    renderPage(gateway({
      loadDailyBatches: vi.fn().mockRejectedValue(new ApiError("不可用", {
        code: "DAILY_UNAVAILABLE",
        status: 503,
      })),
    }))

    expect(await screen.findByText("日线批次暂时无法读取")).toBeInTheDocument()
    expect(screen.getByText("贵州茅台")).toBeInTheDocument()
    expect(screen.getByText(/来源冲突/)).toBeInTheDocument()
    expect(screen.getByText("backfill-1")).toBeInTheDocument()
  })

  it("可展开实时批次逐股诊断并按股票查询复权数据", async () => {
    const marketGateway = gateway()
    renderPage(marketGateway)

    await userEvent.click(await screen.findByRole("button", {
      name: "查看批次 cycle-1 明细",
    }))
    const diagnostic = await screen.findByText("批次逐股诊断")
    expect(within(diagnostic.parentElement?.parentElement as HTMLElement)
      .getByText("1688.00")).toBeInTheDocument()
    expect(marketGateway.loadQuoteItems).toHaveBeenCalledWith("cycle-1")

    await userEvent.type(screen.getByRole("textbox", { name: "股票代码" }), "600519.sh")
    await userEvent.click(screen.getByRole("button", { name: "查询" }))

    expect(await screen.findByText("2010-01-01 至 2026-07-22")).toBeInTheDocument()
    expect(marketGateway.loadQfq).toHaveBeenCalledWith("600519.SH")
  })

  it("质量处置只显示后端许可操作且来源只能从候选项选择", async () => {
    const runQualityAction = vi.fn().mockResolvedValue(undefined)
    renderPage(gateway({ runQualityAction }))

    await userEvent.click(await screen.findByRole("button", { name: "选择来源" }))
    const dialog = screen.getByRole("dialog")
    const sourceSelect = within(dialog).getByRole("combobox", { name: "数据来源" })
    expect(sourceSelect).toHaveTextContent("tushare")
    expect(screen.queryByRole("textbox", { name: "价格" })).not.toBeInTheDocument()

    await userEvent.click(sourceSelect)
    expect(screen.getByRole("option", { name: "tushare" })).toBeInTheDocument()
    await userEvent.click(screen.getByRole("option", { name: "akshare" }))
    await userEvent.type(
      within(dialog).getByRole("textbox", { name: "操作原因" }),
      "人工核对来源",
    )
    await userEvent.click(within(dialog).getByRole("button", { name: "确认处置" }))
    expect(runQualityAction).toHaveBeenCalledWith({
      issueId: "issue-1",
      action: "SELECT_SOURCE",
      reason: "人工核对来源",
      selectedSource: "akshare",
    })
  })

  it("只显示服务端许可的行情写操作", async () => {
    renderPage(gateway({
      loadSecurities: vi.fn().mockResolvedValue({
        ...page([]),
        allowedActions: [],
      }),
      loadQuoteCycles: vi.fn().mockResolvedValue({
        ...page([]),
        allowedActions: ["DIAGNOSE"],
      }),
      loadDailyBatches: vi.fn().mockResolvedValue(page([])),
      loadBackfills: vi.fn().mockResolvedValue({
        ...page([]),
        allowedActions: [],
      }),
    }))

    expect(await screen.findByRole("button", { name: "行情诊断" }))
      .toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "刷新主数据" }))
      .not.toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "手动采集" }))
      .not.toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "新建回填" }))
      .not.toBeInTheDocument()
  })

  it("日线重试提交确认原因且提交期间防止重复操作", async () => {
    let finish: (() => void) | undefined
    const retryDailyBatch = vi.fn().mockImplementation(() => (
      new Promise<void>((resolve) => {
        finish = resolve
      })
    ))
    renderPage(gateway({ retryDailyBatch }))

    await userEvent.click(await screen.findByRole("button", {
      name: "重试缺失",
    }))
    const dialog = screen.getByRole("dialog")
    await userEvent.type(
      within(dialog).getByRole("textbox", { name: "操作原因" }),
      "补齐真实缺失项",
    )
    const confirm = within(dialog).getByRole("button", { name: "确认执行" })
    await userEvent.click(confirm)

    expect(retryDailyBatch).toHaveBeenCalledTimes(1)
    expect(retryDailyBatch).toHaveBeenCalledWith({
      batchId: "batch-1",
      reason: "补齐真实缺失项",
    })
    expect(within(dialog).getByRole("button", { name: "正在提交" }))
      .toBeDisabled()
    finish?.()
  })

  it("历史回填控制携带页面读取到的任务版本", async () => {
    const runBackfillAction = vi.fn().mockResolvedValue(undefined)
    const marketGateway = gateway({ runBackfillAction })
    renderPage(marketGateway)

    await userEvent.click(await screen.findByRole("button", { name: "暂停" }))
    const dialog = screen.getByRole("dialog")
    expect(within(dialog).getByText(/版本 v2/)).toBeInTheDocument()
    await userEvent.type(
      within(dialog).getByRole("textbox", { name: "操作原因" }),
      "为日线批次释放资源",
    )
    await userEvent.click(within(dialog).getByRole("button", {
      name: "确认执行",
    }))

    expect(runBackfillAction).toHaveBeenCalledWith({
      job: expect.objectContaining({ id: "backfill-1", version: 2 }),
      action: "PAUSE",
      reason: "为日线批次释放资源",
    })
  })
})
