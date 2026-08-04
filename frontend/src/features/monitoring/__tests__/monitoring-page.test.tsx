import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import {
  AuthProvider,
  type AuthGateway,
  type AuthState,
} from "@/features/auth"
import {
  MonitoringPage,
  type MonitoringGateway,
  type MonitoringOverview,
} from "@/features/monitoring"
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

function overview(overrides: Partial<MonitoringOverview> = {}): MonitoringOverview {
  return {
    generatedAt: "2026-07-23T02:00:00Z",
    warningCodes: [],
    items: [
      {
        subscriptionId: "sub-1",
        symbol: "600000.SH",
        securityName: "浦发银行",
        groups: ["核心观察"],
        isHolding: true,
        subscriptionStatus: "ENABLED",
        subscriptionVersion: 3,
        scheduleName: "早盘",
        targetMode: "STRATEGY",
        strategyVersionId: "strategy-version-1",
        targetStatus: "READY",
        zone: "NORMAL",
        lastPrice: "10.25",
        lastPriceAt: "2026-07-23T01:45:00Z",
        allowedActions: ["DISABLE", "CHECK_NOW", "DIAGNOSE"],
        warningCodes: [],
      },
      {
        subscriptionId: "sub-2",
        symbol: "600519.SH",
        securityName: "贵州茅台",
        groups: ["长期关注"],
        isHolding: false,
        subscriptionStatus: "PAUSED",
        subscriptionVersion: 2,
        scheduleName: null,
        targetMode: "MANUAL",
        strategyVersionId: null,
        targetStatus: "MISSING",
        zone: "UNKNOWN",
        lastPrice: null,
        lastPriceAt: null,
        allowedActions: ["ENABLE", "ARCHIVE", "DIAGNOSE"],
        warningCodes: [],
      },
    ],
    ...overrides,
  }
}

function renderPage(gateway: MonitoringGateway) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider gateway={authGateway}>
        <MonitoringPage gateway={gateway} />
      </AuthProvider>
    </QueryClientProvider>,
  )
}

function createGateway(
  overrides: Partial<MonitoringGateway> = {},
): MonitoringGateway {
  return {
    loadOverview: vi.fn().mockResolvedValue(overview()),
    loadSchedules: vi.fn().mockResolvedValue([]),
    loadTodaySnapshotStatus: vi.fn().mockResolvedValue({
      overallStatus: "NORMAL",
      plannedCount: 2,
      executedCount: 1,
      fetchedCount: 2,
      items: [{
        executionId: "occurrence-1",
        scheduledTime: "09:45",
        triggerType: "AUTOMATIC",
        status: "SUCCEEDED",
        expectedCount: 2,
        fetchedCount: 2,
        failedCount: 0,
        startedAt: "2026-07-23T01:45:00Z",
        completedAt: "2026-07-23T01:45:03Z",
        durationSeconds: 3,
      }, {
        executionId: "planned:10:30",
        scheduledTime: "10:30",
        triggerType: "AUTOMATIC",
        status: "PENDING",
        expectedCount: 0,
        fetchedCount: 0,
        failedCount: 0,
        startedAt: null,
        completedAt: null,
        durationSeconds: null,
      }],
    }),
    saveSchedule: vi.fn().mockResolvedValue(undefined),
    triggerMarketSnapshot: vi.fn().mockResolvedValue(undefined),
    runAction: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  }
}

describe("监控列表页面", () => {
  it("在顶部展示当天每个监控时间的执行状态", async () => {
    renderPage(createGateway())

    expect(await screen.findByText("今日监控执行状态")).toBeInTheDocument()
    expect(screen.getByText("09:45")).toBeInTheDocument()
    expect(screen.getByText("2 / 2")).toBeInTheDocument()
    expect(screen.getByText("3 秒")).toBeInTheDocument()
  })

  it("可以手动抓取全市场快照并刷新执行记录", async () => {
    const triggerMarketSnapshot = vi.fn().mockResolvedValue(undefined)
    const loadTodaySnapshotStatus = vi.fn().mockResolvedValue({
      overallStatus: "NORMAL",
      plannedCount: 0,
      executedCount: 1,
      fetchedCount: 5529,
      items: [{
        executionId: "manual-1",
        scheduledTime: "10:18",
        triggerType: "MANUAL",
        status: "SUCCEEDED",
        expectedCount: 5529,
        fetchedCount: 5529,
        failedCount: 0,
        startedAt: "2026-07-23T02:18:00Z",
        completedAt: "2026-07-23T02:18:03Z",
        durationSeconds: 3,
      }],
    })
    renderPage(createGateway({ triggerMarketSnapshot, loadTodaySnapshotStatus }))

    await screen.findByText("今日监控执行状态")
    await userEvent.click(screen.getByRole("button", { name: "手动抓取快照" }))

    expect(triggerMarketSnapshot).toHaveBeenCalledTimes(1)
    expect(await screen.findByText("手动")).toBeInTheDocument()
    expect(loadTodaySnapshotStatus).toHaveBeenCalledTimes(2)
  })

  it("展示中文监控信息，并支持持仓筛选和搜索", async () => {
    renderPage(createGateway({
      loadOverview: vi.fn().mockResolvedValue(overview()),
    }))

    expect(await screen.findByText("浦发银行")).toBeInTheDocument()
    expect(screen.getByText("¥ 10.25")).toBeInTheDocument()
    expect(screen.getByText("正常区间")).toBeInTheDocument()
    expect(screen.getByText("策略目标")).toBeInTheDocument()

    await userEvent.click(screen.getByRole("radio", { name: "持仓" }))
    expect(screen.getByText("浦发银行")).toBeInTheDocument()
    expect(screen.queryByText("贵州茅台")).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole("radio", { name: "全部" }))
    await userEvent.type(
      screen.getByRole("textbox", { name: "搜索股票、名称或分组" }),
      "长期",
    )
    expect(screen.getByText("贵州茅台")).toBeInTheDocument()
    expect(screen.queryByText("浦发银行")).not.toBeInTheDocument()

    await userEvent.clear(
      screen.getByRole("textbox", { name: "搜索股票、名称或分组" }),
    )
    await userEvent.click(
      screen.getByRole("combobox", { name: "按目标模式筛选" }),
    )
    await userEvent.click(screen.getByRole("option", { name: "策略目标" }))
    expect(screen.getByText("浦发银行")).toBeInTheDocument()
    expect(screen.queryByText("贵州茅台")).not.toBeInTheDocument()
  })

  it("辅助数据局部失败时保留订阅，并明确显示降级提示", async () => {
    const value = overview({
      warningCodes: ["POSITIONS_UNAVAILABLE"],
      items: [
        {
          ...overview().items[0],
          warningCodes: ["SECURITY_DETAIL_UNAVAILABLE"],
        },
      ],
    })
    renderPage(createGateway({
      loadOverview: vi.fn().mockResolvedValue(value),
    }))

    expect(await screen.findByText("浦发银行")).toBeInTheDocument()
    expect(screen.getByText("部分辅助数据暂不可用，股票订阅仍可正常查看。"))
      .toBeInTheDocument()
    expect(screen.getByLabelText("该股票部分数据暂不可用")).toBeInTheDocument()
  })

  it("整体失败后可以重试恢复", async () => {
    const loadOverview = vi
      .fn()
      .mockRejectedValueOnce(new ApiError("暂不可用", {
        code: "MONITORING_UNAVAILABLE",
        status: 503,
      }))
      .mockResolvedValueOnce(overview({ items: [] }))
    renderPage(createGateway({ loadOverview }))

    expect(await screen.findByText("监控列表暂时无法读取")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "重新加载监控列表" }))

    expect(await screen.findByText("还没有监控股票")).toBeInTheDocument()
    expect(loadOverview).toHaveBeenCalledTimes(2)
  })

  it("只展示后端允许的操作，并携带版本和原因执行", async () => {
    const runAction = vi.fn().mockResolvedValue(undefined)
    renderPage(createGateway({
      loadOverview: vi.fn().mockResolvedValue(overview()),
      runAction,
    }))

    expect(await screen.findByRole("button", { name: "暂停监控" }))
      .toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "归档订阅" }))
      .toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "立即检查" }))
    await userEvent.type(
      screen.getByRole("textbox", { name: "操作原因" }),
      "人工复核最新行情",
    )
    await userEvent.click(screen.getByRole("button", { name: "确认执行" }))

    expect(runAction).toHaveBeenCalledWith(
      "sub-1",
      "CHECK_NOW",
      3,
      "人工复核最新行情",
    )
  })

  it("可以修改盘中监控时间并保存原因", async () => {
    const saveSchedule = vi.fn().mockResolvedValue(undefined)
    renderPage(createGateway({
      loadSchedules: vi.fn().mockResolvedValue([
        { id: "schedule-1", name: "盘中监控", version: 2, times: ["09:45", "10:30"] },
      ]),
      saveSchedule,
    }))

    expect(await screen.findByDisplayValue("09:45")).toBeInTheDocument()
    await userEvent.type(
      screen.getByRole("textbox", { name: "修改原因" }),
      "调整盘中检查频率",
    )
    await userEvent.click(screen.getByRole("button", { name: "保存监控时间" }))

    expect(saveSchedule).toHaveBeenCalledWith({
      id: "schedule-1",
      name: "盘中监控",
      version: 2,
      times: ["09:45", "10:30"],
      reason: "调整盘中检查频率",
    })
  })
})
