import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"
import { MemoryRouter } from "react-router-dom"

import {
  AuthProvider,
  type AuthGateway,
  type AuthState,
} from "@/features/auth"
import {
  DashboardPage,
  type DashboardGateway,
  type DashboardSection,
  type DashboardSummary,
} from "@/features/dashboard"
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

function section(
  data: Record<string, unknown>,
  overrides: Partial<DashboardSection> = {},
): DashboardSection {
  return {
    status: "OK",
    updated_at: "2026-07-23T02:00:00Z",
    data,
    error: null,
    ...overrides,
  }
}

function summary(): DashboardSummary {
  return {
    status: "HEALTHY",
    generated_at: "2026-07-23T02:00:00Z",
    sections: {
      system: section({ open_alerts: 2, critical_alerts: 1 }),
      quote_batches: section({ valid_count: 88, expected_count: 100 }),
      monitoring: section({ active: 12, with_current_state: 10, missing_state: 2 }),
      positions: section({ held: 4 }),
      signals: section({ today: 3, low_zone: 4, high_zone: 2 }),
      daily_data: section({
        trading_date: "2026-07-22",
        status: "SUCCEEDED",
        expected_count: 5529,
        committed_count: 5529,
        missing_count: 0,
        failed_count: 0,
      }),
      targets: section({ attention: 2 }),
      jobs: section({ active: 5 }),
      notifications: section({ pending: 6 }),
      providers: section({ healthy: 2 }),
      infrastructure: section({ active_workers: 9 }),
      alerts: section({ unresolved: 2 }),
    },
  }
}

function renderDashboard(gateway: DashboardGateway) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider gateway={authGateway}>
        <MemoryRouter>
          <DashboardPage gateway={gateway} />
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  )
}

describe("真实仪表盘", () => {
  it("只显示接口返回的指标，并保留图标卡的无障碍名称", async () => {
    renderDashboard({ loadSummary: vi.fn().mockResolvedValue(summary()) })

    expect(await screen.findByText("运行正常")).toBeInTheDocument()
    expect(screen.getByLabelText("启用监控：12，状态正常")).toHaveTextContent("12启用监控")
    expect(screen.getByLabelText("日线提交：5529，状态正常")).toHaveTextContent("5529日线提交")
    expect(screen.getByLabelText("严重告警：1，状态正常")).toHaveTextContent("1严重告警")
    expect(screen.getByRole("link", { name: /启用监控.*查看详情/ }))
      .toHaveAttribute("href", "/monitoring")
    expect(screen.getByRole("link", { name: /今日信号.*查看详情/ }))
      .toHaveAttribute("href", "/signals")
    expect(screen.getByText("监控覆盖情况")).toBeInTheDocument()
    expect(screen.getByText("信号区间分布")).toBeInTheDocument()
    expect(screen.getByText("最近交易日日线")).toBeInTheDocument()
    expect(screen.getByRole("link", { name: "查看日线批次" }))
      .toHaveAttribute("href", "/market-data")
  })

  it("单个分区超时只降级对应卡片，不阻断其他指标", async () => {
    const value = summary()
    value.status = "DEGRADED"
    value.sections.notifications = section({}, {
      status: "TIMEOUT",
      error: "section deadline exceeded",
    })
    renderDashboard({ loadSummary: vi.fn().mockResolvedValue(value) })

    expect(await screen.findByText("部分降级")).toBeInTheDocument()
    expect(screen.getByLabelText("待发通知：无数据，状态超时")).toHaveTextContent("—待发通知")
    expect(screen.getByLabelText("启用监控：12，状态正常")).toBeInTheDocument()
  })

  it("整体请求失败显示稳定错误码并允许重试", async () => {
    const loadSummary = vi
      .fn()
      .mockRejectedValueOnce(new ApiError("unavailable", {
        code: "DASHBOARD_UNAVAILABLE",
        status: 503,
      }))
      .mockResolvedValueOnce(summary())
    renderDashboard({ loadSummary })

    expect(await screen.findByText("DASHBOARD_UNAVAILABLE")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "重试仪表盘" }))

    expect(await screen.findByText("运行正常")).toBeInTheDocument()
    expect(loadSummary).toHaveBeenCalledTimes(2)
  })
})
