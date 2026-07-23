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
    loadSecurities: vi.fn().mockResolvedValue(page([{
      id: "600519.SH",
      symbol: "600519.SH",
      name: "贵州茅台",
      market: "主板",
      listingStatus: "LISTED",
      isSt: false,
      isSuspended: false,
      masterVersion: 12,
      updatedAt: "2026-07-23T02:00:00Z",
    }])),
    loadQuoteCycles: vi.fn().mockResolvedValue(page([{
      id: "cycle-1",
      status: "PARTIAL",
      expectedCount: 2,
      validCount: 1,
      missingCount: 1,
      conflictCount: 0,
      failedCount: 0,
      scheduledAt: "2026-07-23T02:00:00Z",
      finalizedAt: "2026-07-23T02:00:30Z",
    }])),
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
    }])),
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
    }),
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
    loadBackfills: vi.fn().mockResolvedValue(page([{
      id: "backfill-1",
      status: "RUNNING",
      version: 2,
      completed: 80,
      total: 100,
      succeeded: null,
      failed: null,
      updatedAt: "2026-07-23T02:00:00Z",
      terminalAt: null,
    }])),
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
    expect(sourceSelect).toHaveTextContent("akshare")
    expect(screen.queryByRole("textbox", { name: "价格" })).not.toBeInTheDocument()

    await userEvent.selectOptions(sourceSelect, "akshare")
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
})
