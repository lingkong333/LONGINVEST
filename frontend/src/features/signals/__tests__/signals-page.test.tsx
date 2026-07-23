import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import {
  AuthProvider,
  type AuthGateway,
  type AuthState,
  useAuth,
} from "@/features/auth"
import {
  SignalsPage,
  type SignalEvaluation,
  type SignalEventItem,
  type SignalState,
  type SignalsGateway,
} from "@/features/signals"
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

const states: SignalState[] = [
  {
    subscription_id: "subscription-low",
    zone: "LOW",
    version: 3,
    last_price: "8.10",
    last_price_at: "2026-07-23T02:50:00Z",
  },
  {
    subscription_id: "subscription-high",
    zone: "STRONG_HIGH",
    version: 2,
    last_price: "13.50",
    last_price_at: "2026-07-23T02:40:00Z",
  },
]

const events: SignalEventItem[] = [{
  id: "signal-event-1",
  subscription_id: "subscription-low",
  evaluation_id: "evaluation-1",
  before_zone: "NORMAL",
  after_zone: "LOW",
  reason: "SCHEDULED_QUOTE",
  price: "8.10",
  price_at: "2026-07-23T02:50:00Z",
  targets: {
    low_strong: "7.00",
    low_watch: "8.20",
    high_watch: "12.00",
    high_strong: "13.00",
  },
  target_revision_id: "target-1",
  target_version: 2,
  target_date: "2026-07-22",
  position_status: "NOT_HOLDING",
  position_version: 1,
  used_stale_target: false,
  state_version: 3,
  notification_class: "LOW",
  notification_eligible: true,
  suppression_reason: null,
  created_at: "2026-07-23T02:50:01Z",
  notificationStatus: "DELIVERED",
  deliveries: [{
    id: "delivery-1",
    channel: "WECOM",
    status: "SENT",
    sentAt: "2026-07-23T02:50:05Z",
    errorCode: null,
  }],
}]

const evaluations: SignalEvaluation[] = [
  {
    id: "evaluation-unchanged",
    subscription_id: "subscription-low",
    reason: "SCHEDULED_QUOTE",
    result: "UNCHANGED",
    before_zone: "LOW",
    after_zone: "LOW",
    price: "8.15",
    hysteresis_applied: true,
    used_stale_target: false,
    content_hash: "hash-1",
    created_at: "2026-07-23T03:00:00Z",
  },
  {
    id: "evaluation-superseded",
    subscription_id: "subscription-low",
    reason: "RECOVERY_REEVALUATION",
    result: "SUPERSEDED",
    before_zone: "LOW",
    after_zone: "LOW",
    price: null,
    hysteresis_applied: false,
    used_stale_target: true,
    skip_code: "STALE_QUOTE",
    content_hash: "hash-2",
    created_at: "2026-07-23T02:00:00Z",
  },
]

function gateway(overrides: Partial<SignalsGateway> = {}): SignalsGateway {
  return {
    loadStates: vi.fn().mockResolvedValue({
      items: states,
      page: 1,
      pageSize: 20,
      total: 2,
    }),
    loadEvents: vi.fn().mockResolvedValue({
      items: events,
      page: 1,
      pageSize: 20,
      total: 1,
      warningCodes: [],
    }),
    loadEvaluations: vi.fn().mockResolvedValue({
      items: evaluations,
      page: 1,
      pageSize: 20,
      total: 2,
    }),
    ...overrides,
  }
}

function AuthPhaseProbe() {
  return <output aria-label="登录阶段">{useAuth().phase}</output>
}

function renderPage(signals: SignalsGateway, withProbe = false) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider gateway={authGateway}>
        {withProbe ? <AuthPhaseProbe /> : null}
        <SignalsPage gateway={signals} />
      </AuthProvider>
    </QueryClientProvider>,
  )
}

describe("信号中心页面", () => {
  it("展示当前状态并支持区间筛选", async () => {
    renderPage(gateway())

    expect(await screen.findByText("¥ 8.10")).toBeInTheDocument()
    expect(screen.getByText("¥ 13.50")).toBeInTheDocument()
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "按信号区间筛选" }),
      "LOW",
    )
    expect(screen.getByText("¥ 8.10")).toBeInTheDocument()
    expect(screen.queryByText("¥ 13.50")).not.toBeInTheDocument()
  })

  it("分开展示事件详情、通知资格和投递结果", async () => {
    renderPage(gateway())
    await screen.findByText("¥ 8.10")

    await userEvent.click(screen.getByRole("button", { name: /信号事件/ }))

    expect(screen.getByText("正常区间")).toBeInTheDocument()
    expect(screen.getByText("低位")).toBeInTheDocument()
    expect(screen.getAllByText("定时行情")).toHaveLength(2)
    expect(screen.getAllByText("符合通知条件")).toHaveLength(2)
    expect(screen.getByText("已送达")).toBeInTheDocument()
    expect(screen.getByText("企业微信：已发送")).toBeInTheDocument()
  })

  it("判断记录包含同区间、跳过信息和过期结果", async () => {
    renderPage(gateway())
    await screen.findByText("¥ 8.10")

    await userEvent.click(screen.getByRole("button", { name: /判断记录/ }))

    expect(screen.getAllByText("仍在同一区间")).toHaveLength(2)
    expect(screen.getAllByText("已过期")).toHaveLength(2)
    expect(screen.getByText("跳过原因：STALE_QUOTE")).toBeInTheDocument()
    expect(screen.getByText("使用了待更新目标")).toBeInTheDocument()
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "按判断结果筛选" }),
      "SUPERSEDED",
    )
    expect(screen.getAllByText("仍在同一区间")).toHaveLength(1)
    expect(screen.getAllByText("已过期")).toHaveLength(2)
  })

  it("一个分区失败不影响其他分区，并可以单独重试", async () => {
    const loadEvents = vi
      .fn()
      .mockRejectedValueOnce(new ApiError("事件暂不可用", {
        code: "SIGNAL_EVENTS_UNAVAILABLE",
        status: 503,
      }))
      .mockResolvedValueOnce({
        items: [],
        page: 1,
        pageSize: 20,
        total: 0,
        warningCodes: [],
      })
    renderPage(gateway({ loadEvents }))

    expect(await screen.findByText("¥ 8.10")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: /信号事件/ }))
    expect(await screen.findByText("信号事件暂时无法读取")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "重新加载" }))
    expect(await screen.findByText("暂无信号事件")).toBeInTheDocument()
    expect(loadEvents).toHaveBeenCalledTimes(2)
  })

  it("通知辅助数据不完整时明确提示但保留事件", async () => {
    renderPage(gateway({
      loadEvents: vi.fn().mockResolvedValue({
        items: events,
        page: 1,
        pageSize: 20,
        total: 1,
        warningCodes: ["NOTIFICATION_DELIVERIES_UNAVAILABLE"],
      }),
    }))
    await screen.findByText("¥ 8.10")

    await userEvent.click(screen.getByRole("button", { name: /信号事件/ }))

    expect(screen.getByText("通知投递数据暂时不完整，信号事件仍可正常查看。"))
      .toBeInTheDocument()
    expect(screen.getByText("¥ 8.10")).toBeInTheDocument()
  })

  it("任何分区返回登录失效时使当前会话失效", async () => {
    renderPage(gateway({
      loadEvaluations: vi.fn().mockRejectedValue(
        new ApiError("登录失效", { code: "UNAUTHORIZED", status: 401 }),
      ),
    }), true)

    await waitFor(() => {
      expect(screen.getByLabelText("登录阶段")).toHaveTextContent("unauthenticated")
    })
  })
})
