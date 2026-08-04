import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { describe, expect, it, vi } from "vitest"

import { marketDataGateway } from "@/features/market-data/gateway"
import { HistoryBackfillPage } from "@/features/market-data/history-backfill-page"

describe("历史补全逐股明细", () => {
  it("支持勾选异常股票并指定来源批量重试", async () => {
    const retryBackfillItems = vi.fn().mockResolvedValue(undefined)
    const gateway = {
      ...marketDataGateway,
      loadBackfillItems: vi.fn().mockResolvedValue({
        items: [{
          securityId: "security-1",
          symbol: "600000.SH",
          status: "ANOMALY" as const,
          errorCode: null,
          retryable: true,
          anomalyRows: [{
            trade_date: "2020-01-03",
            error_code: "HISTORY_BAR_OHLC_INVALID",
            price_mode: "UNADJUSTED",
          }],
        }],
        pagination: { page: 1, pageSize: 100, total: 1 },
      }),
      retryBackfillItems,
    }
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={["/market-data/backfills/job-1"]}>
          <Routes>
            <Route
              path="/market-data/backfills/:jobId"
              element={<HistoryBackfillPage gateway={gateway} />}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )

    expect(await screen.findByText("1 个异常交易日已跳过")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("checkbox", { name: "选择 600000.SH" }))
    await userEvent.click(screen.getByRole("button", { name: "批量重试 (1)" }))
    await userEvent.click(screen.getByRole("combobox", { name: "数据源" }))
    await userEvent.click(screen.getByRole("option", { name: "新浪" }))
    await userEvent.click(screen.getByRole("button", { name: "确认重试" }))

    expect(retryBackfillItems).toHaveBeenCalledWith({
      jobId: "job-1",
      symbols: ["600000.SH"],
      providerCode: "SINA",
      concurrency: 4,
      reason: "管理员从历史补全明细重试",
    })
  })
})
