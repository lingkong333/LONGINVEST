import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { describe, expect, it, vi } from "vitest"

import {
  SecurityDetailPage,
  SecurityListPage,
  type MarketDataGateway,
} from "@/features/market-data"

function gateway(
  overrides: Partial<MarketDataGateway> = {},
): MarketDataGateway {
  return {
    loadSecurities: vi.fn().mockResolvedValue({
      items: [],
      pagination: { page: 1, pageSize: 50, total: 0 },
      allowedActions: [],
    }),
    loadSecurityList: vi.fn().mockResolvedValue({
      items: [],
      pagination: { page: 1, pageSize: 50, total: 0 },
    }),
    loadSecurity: vi.fn().mockRejectedValue(new Error("not used")),
    loadDailyPrices: vi.fn().mockResolvedValue({
      symbol: "600519.SH",
      mode: "UNADJUSTED",
      items: [],
      total: 0,
    }),
    refreshSecurities: vi.fn().mockResolvedValue(undefined),
    loadQuoteCycles: vi.fn().mockResolvedValue({
      items: [],
      pagination: { page: 1, pageSize: 50, total: 0 },
      allowedActions: [],
    }),
    loadQuoteItems: vi.fn().mockResolvedValue([]),
    runQuoteOperation: vi.fn().mockResolvedValue(undefined),
    loadDailyBatches: vi.fn().mockResolvedValue({
      items: [],
      pagination: { page: 1, pageSize: 50, total: 0 },
    }),
    retryDailyBatch: vi.fn().mockResolvedValue(undefined),
    loadQfq: vi.fn().mockRejectedValue(new Error("not used")),
    refreshQfq: vi.fn().mockResolvedValue(undefined),
    loadQualityIssues: vi.fn().mockResolvedValue({
      items: [],
      pagination: { page: 1, pageSize: 50, total: 0 },
    }),
    runQualityAction: vi.fn().mockResolvedValue(undefined),
    loadBackfills: vi.fn().mockResolvedValue({
      items: [],
      pagination: { page: 1, pageSize: 50, total: 0 },
      allowedActions: [],
    }),
    createBackfill: vi.fn().mockResolvedValue(undefined),
    runBackfillAction: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  }
}

function renderRoute(
  element: React.ReactNode,
  {
    initialEntry,
    path,
  }: { initialEntry: string; path: string },
) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path={path} element={element} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe("全部 A 股列表", () => {
  it("使用 URL 搜索和服务端分页并提供详情入口", async () => {
    const loadSecurityList = vi.fn().mockResolvedValue({
      items: [{
        id: "600519.SH",
        symbol: "600519.SH",
        name: "贵州茅台",
        market: "SH",
        listingStatus: "LISTED",
        isSt: false,
        isSuspended: false,
        masterVersion: 2,
        updatedAt: "2026-07-23T03:00:00Z",
      }],
      pagination: { page: 2, pageSize: 50, total: 51 },
    })
    renderRoute(
      <SecurityListPage gateway={gateway({ loadSecurityList })} />,
      {
        initialEntry: "/market-data/stocks?q=茅台&page=2",
        path: "/market-data/stocks",
      },
    )

    expect(await screen.findByText("贵州茅台")).toBeInTheDocument()
    expect(loadSecurityList).toHaveBeenCalledWith({
      query: "茅台",
      page: 2,
      pageSize: 50,
    })
    expect(screen.getByRole("link", {
      name: "查看 贵州茅台 日线详情",
    })).toHaveAttribute("href", "/market-data/stocks/600519.SH")
    expect(screen.getByRole("button", { name: "上一页" })).toBeEnabled()
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled()
  })
})

describe("股票日线详情", () => {
  it("展示开收盘线并按 0.01 调整观察线和切换复权", async () => {
    const loadDailyPrices = vi.fn().mockImplementation(
      ({ symbol, mode }: { symbol: string; mode: string }) => Promise.resolve({
        symbol,
        mode,
        total: 2,
        items: [{
          tradeDate: "2026-07-21",
          open: "11.00",
          high: "12.00",
          low: "10.00",
          close: "11.50",
          volume: 1000,
          amount: "11500.00",
        }, {
          tradeDate: "2026-07-22",
          open: "11.50",
          high: "13.00",
          low: "10.50",
          close: "12.50",
          volume: 1200,
          amount: "15000.00",
        }],
      }),
    )
    renderRoute(
      <SecurityDetailPage gateway={gateway({
        loadSecurity: vi.fn().mockResolvedValue({
          id: "600519.SH",
          symbol: "600519.SH",
          exchangeCode: "600519",
          name: "贵州茅台",
          market: "SH",
          securityType: "A_SHARE",
          listedOn: "2001-08-27",
          delistedOn: null,
          listingStatus: "LISTED",
          isSt: false,
          isSuspended: false,
          masterVersion: 2,
          updatedAt: "2026-07-23T03:00:00Z",
        }),
        loadDailyPrices,
      })} />,
      {
        initialEntry: "/market-data/stocks/600519.SH",
        path: "/market-data/stocks/:symbol",
      },
    )

    expect(await screen.findByText("贵州茅台")).toBeInTheDocument()
    expect(await screen.findByLabelText(
      "股票开盘价和收盘价日线图",
    )).toBeInTheDocument()
    expect(await screen.findByDisplayValue("10.00")).toBeInTheDocument()
    expect(screen.getByDisplayValue("13.00")).toBeInTheDocument()
    expect(screen.getByText("实线")).toBeInTheDocument()
    expect(screen.getByText("虚线")).toBeInTheDocument()

    await userEvent.click(screen.getByRole("button", {
      name: "支撑位增加 0.01 元",
    }))
    expect(await screen.findByDisplayValue("10.01")).toBeInTheDocument()

    await userEvent.click(screen.getByRole("radio", { name: "前复权" }))
    await waitFor(() => expect(loadDailyPrices).toHaveBeenLastCalledWith(
      expect.objectContaining({ symbol: "600519.SH", mode: "QFQ" }),
    ))

    const supportGroup = screen.getByLabelText("支撑位").parentElement
    expect(within(supportGroup as HTMLElement).getByDisplayValue("10.01"))
      .toBeInTheDocument()
  })

  it("阻止开始日期晚于结束日期", async () => {
    const loadDailyPrices = vi.fn().mockResolvedValue({
      symbol: "600519.SH",
      mode: "UNADJUSTED",
      items: [],
      total: 0,
    })
    renderRoute(
      <SecurityDetailPage
        symbol="600519.SH"
        gateway={gateway({
          loadSecurity: vi.fn().mockResolvedValue({
            id: "600519.SH",
            symbol: "600519.SH",
            exchangeCode: "600519",
            name: "贵州茅台",
            market: "SH",
            securityType: "A_SHARE",
            listedOn: "2001-08-27",
            delistedOn: null,
            listingStatus: "LISTED",
            isSt: false,
            isSuspended: false,
            masterVersion: 2,
            updatedAt: "2026-07-23T03:00:00Z",
          }),
          loadDailyPrices,
        })}
      />,
      {
        initialEntry: "/market-data/stocks/600519.SH",
        path: "/market-data/stocks/:symbol",
      },
    )
    await screen.findByText("贵州茅台")
    const initialCalls = loadDailyPrices.mock.calls.length
    await userEvent.clear(screen.getByLabelText("开始日期"))
    await userEvent.type(screen.getByLabelText("开始日期"), "2026-08-01")
    await userEvent.clear(screen.getByLabelText("结束日期"))
    await userEvent.type(screen.getByLabelText("结束日期"), "2026-07-01")
    await userEvent.click(screen.getByRole("button", { name: "应用范围" }))

    expect(screen.getByRole("alert")).toHaveTextContent(
      "开始日期不能晚于结束日期",
    )
    expect(loadDailyPrices).toHaveBeenCalledTimes(initialCalls)
  })
})
