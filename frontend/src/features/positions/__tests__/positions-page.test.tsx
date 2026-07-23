import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import {
  AuthProvider,
  type AuthGateway,
  type AuthState,
} from "@/features/auth"
import {
  PositionsPage,
  type PositionGateway,
  type PositionOverview,
} from "@/features/positions"
import { ApiError } from "@/shared/api/client"

const authState: AuthState = {
  user: { id: "user-1", username: "admin", status: "ACTIVE" },
  session: {
    id: "session-1",
    status: "ACTIVE",
    current: true,
    created_at: "2026-07-23T00:00:00Z",
    last_request_at: "2026-07-23T00:00:00Z",
    last_user_activity_at: "2026-07-23T00:00:00Z",
    absolute_expires_at: "2026-10-23T00:00:00Z",
    ip_summary: null,
    user_agent_summary: null,
  },
}

const authGateway: AuthGateway = {
  loadSession: vi.fn().mockResolvedValue(authState),
  login: vi.fn().mockResolvedValue(authState),
  logout: vi.fn().mockResolvedValue(undefined),
  setUnauthorizedHandler: vi.fn(),
}

function overview(overrides: Partial<PositionOverview> = {}): PositionOverview {
  return {
    warningCodes: [],
    items: [
      {
        securityId: "security-1",
        symbol: "600000.SH",
        securityName: "浦发银行",
        status: "HOLDING",
        version: 3,
        source: "manual",
        updatedAt: "2026-07-23T02:00:00Z",
        isMonitored: false,
        allowedActions: ["CLEAR"],
        warningCodes: [],
      },
      {
        securityId: "security-2",
        symbol: "600519.SH",
        securityName: "贵州茅台",
        status: "NOT_HOLDING",
        version: 2,
        source: "manual",
        updatedAt: "2026-07-22T02:00:00Z",
        isMonitored: true,
        allowedActions: ["HOLD"],
        warningCodes: [],
      },
    ],
    ...overrides,
  }
}

function gateway(overrides: Partial<PositionGateway> = {}): PositionGateway {
  return {
    loadCurrent: vi.fn().mockResolvedValue(overview()),
    loadHistory: vi.fn().mockResolvedValue([]),
    changePosition: vi.fn().mockResolvedValue(undefined),
    changeBatch: vi.fn().mockResolvedValue([]),
    ...overrides,
  }
}

function renderPage(positionGateway: PositionGateway) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider gateway={authGateway}>
        <PositionsPage gateway={positionGateway} />
      </AuthProvider>
    </QueryClientProvider>,
  )
}

describe("持仓管理页面", () => {
  it("以中文显示当前状态、业务边界和未监控快捷入口", async () => {
    renderPage(gateway())

    expect(await screen.findByText("浦发银行")).toBeInTheDocument()
    expect(screen.getByText("这里只记录股票是否持有，不保存数量、成本、成交记录和真实盈亏。", {
      exact: false,
    })).toBeInTheDocument()
    expect(screen.getByText("已持仓 · 未监控")).toBeInTheDocument()
    expect(screen.getByRole("link", { name: "前往加入监控" }))
      .toHaveAttribute("href", "/monitoring?symbol=600000.SH")
    expect(screen.getByRole("button", { name: "标记清仓" })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "标记持仓" })).toBeInTheDocument()
  })

  it("单只修改必须确认并提交原因、备注和当前版本", async () => {
    const changePosition = vi.fn().mockResolvedValue(undefined)
    renderPage(gateway({ changePosition }))

    await userEvent.click(await screen.findByRole("button", { name: "标记清仓" }))
    const dialog = screen.getByRole("dialog")
    const confirm = within(dialog).getByRole("button", { name: "确认修改" })
    expect(confirm).toBeDisabled()

    await userEvent.type(
      within(dialog).getByRole("textbox", { name: "操作原因" }),
      "已经卖出",
    )
    await userEvent.type(
      within(dialog).getByRole("textbox", { name: "备注（可选）" }),
      "人工核对",
    )
    await userEvent.click(confirm)

    expect(changePosition).toHaveBeenCalledWith({
      symbol: "600000.SH",
      action: "CLEAR",
      expectedVersion: 3,
      reason: "已经卖出",
      note: "人工核对",
    })
    expect(await screen.findByText("持仓状态已更新。")).toBeInTheDocument()
  })

  it("批量按钮只在所有选中股票均获后端允许时启用", async () => {
    const changeBatch = vi.fn().mockResolvedValue([
      { symbol: "600000.SH", status: "CHANGED", code: "POSITION_CHANGED" },
    ])
    renderPage(gateway({ changeBatch }))

    await screen.findByText("浦发银行")
    const batchClear = screen.getByRole("button", { name: "批量标记清仓" })
    expect(batchClear).toBeDisabled()
    await userEvent.click(screen.getByRole("checkbox", { name: "选择 600000.SH" }))
    expect(batchClear).toBeEnabled()
    await userEvent.click(screen.getByRole("checkbox", { name: "选择 600519.SH" }))
    expect(batchClear).toBeDisabled()
  })

  it("历史标签只读展示状态变化和分页", async () => {
    const history = Array.from({ length: 13 }, (_, index) => ({
      id: `history-${index}`,
      symbol: "600000.SH",
      beforeStatus: index === 0 ? null : "NOT_HOLDING" as const,
      afterStatus: "HOLDING" as const,
      version: index + 1,
      note: index === 0 ? "首次记录" : null,
      source: "manual",
      requestId: `req-${index}`,
      effectiveAt: "2026-07-23T02:00:00Z",
    }))
    renderPage(gateway({
      loadHistory: vi.fn().mockResolvedValue(history),
    }))

    await userEvent.click(screen.getByRole("tab", { name: "修改历史" }))

    expect(await screen.findByText("共 13 条，第 1 / 2 页")).toBeInTheDocument()
    expect(screen.getAllByText("600000.SH")).toHaveLength(12)
    await userEvent.click(screen.getByRole("button", { name: "下一页" }))
    expect(screen.getByText("共 13 条，第 2 / 2 页")).toBeInTheDocument()
    expect(screen.getAllByText("600000.SH")).toHaveLength(1)
  })

  it("版本冲突时保留确认窗口并提示重新加载", async () => {
    renderPage(gateway({
      changePosition: vi.fn().mockRejectedValue(new ApiError("版本冲突", {
        code: "POSITION_VERSION_CONFLICT",
        status: 409,
      })),
    }))

    await userEvent.click(await screen.findByRole("button", { name: "标记清仓" }))
    const dialog = screen.getByRole("dialog")
    await userEvent.type(
      within(dialog).getByRole("textbox", { name: "操作原因" }),
      "状态核对",
    )
    await userEvent.click(within(dialog).getByRole("button", { name: "确认修改" }))

    expect(await within(dialog).findByText(
      "持仓状态已被其他操作修改。请关闭窗口并重新加载后再试。",
    )).toBeInTheDocument()
    expect(dialog).toBeInTheDocument()
  })

  it("空数据和加载失败均提供明确反馈", async () => {
    const loadCurrent = vi
      .fn()
      .mockRejectedValueOnce(new ApiError("暂不可用", {
        code: "POSITIONS_UNAVAILABLE",
        status: 503,
      }))
      .mockResolvedValueOnce(overview({ items: [] }))
    renderPage(gateway({ loadCurrent }))

    expect(await screen.findByText("持仓状态暂时无法读取")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "重新加载持仓" }))
    expect(await screen.findByText("还没有持仓状态记录")).toBeInTheDocument()
  })
})
