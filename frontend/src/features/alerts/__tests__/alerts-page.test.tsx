import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import {
  type AlertGateway,
  type AlertItem,
  AlertsPage,
} from "@/features/alerts"
import {
  AuthProvider,
  type AuthGateway,
  type AuthState,
} from "@/features/auth"
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

function alert(overrides: Partial<AlertItem> = {}): AlertItem {
  return {
    id: "alert-1",
    aggregationKey: "worker:worker-1",
    alertType: "WORKER_TIMEOUT",
    objectType: "worker",
    objectId: "worker-1",
    severity: "ERROR",
    status: "OPEN",
    title: "行情进程超时",
    summary: "行情进程已超过心跳期限",
    details: { worker: "worker-1" },
    occurrenceCount: 2,
    firstSeenAt: "2026-07-23T01:00:00Z",
    lastSeenAt: "2026-07-23T02:00:00Z",
    acknowledgedAt: null,
    acknowledgedByUserId: null,
    resolvedAt: null,
    resolvedByUserId: null,
    resolutionReason: null,
    version: 3,
    createdAt: "2026-07-23T01:00:00Z",
    updatedAt: "2026-07-23T02:00:00Z",
    allowedActions: ["ACKNOWLEDGE", "RESOLVE", "RETRY"],
    ...overrides,
  }
}

function gateway(overrides: Partial<AlertGateway> = {}): AlertGateway {
  return {
    loadAlerts: vi.fn().mockResolvedValue({
      items: [alert()],
      total: 1,
      page: 1,
      pageSize: 20,
    }),
    loadAlert: vi.fn().mockResolvedValue(alert()),
    loadOccurrences: vi.fn().mockResolvedValue({
      items: [{
        id: "occurrence-1",
        alertId: "alert-1",
        sourceEventId: "event-1",
        severity: "ERROR",
        summary: "再次超时",
        details: {},
        requestId: "req-occurrence",
        occurredAt: "2026-07-23T02:00:00Z",
      }],
      total: 1,
      page: 1,
      pageSize: 50,
    }),
    loadActions: vi.fn().mockResolvedValue({
      items: [{
        id: "action-1",
        alertId: "alert-1",
        action: "OPENED",
        reason: null,
        actorUserId: null,
        requestId: "req-action",
        jobId: null,
        createdAt: "2026-07-23T01:00:00Z",
      }],
      total: 1,
      page: 1,
      pageSize: 50,
    }),
    runAction: vi.fn().mockResolvedValue({ alert: alert(), jobId: null }),
    ...overrides,
  }
}

function renderPage(value: AlertGateway) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider gateway={authGateway}>
        <AlertsPage gateway={value} />
      </AuthProvider>
    </QueryClientProvider>,
  )
}

describe("系统告警页面", () => {
  it("显示加载状态后展示列表，并提交筛选条件", async () => {
    let finish: ((value: Awaited<ReturnType<AlertGateway["loadAlerts"]>>) => void) | undefined
    const loadAlerts = vi.fn().mockReturnValue(new Promise((resolve) => {
      finish = resolve
    }))
    renderPage(gateway({ loadAlerts }))

    expect(screen.getByText("正在读取系统告警")).toBeInTheDocument()
    finish?.({ items: [alert()], total: 1, page: 1, pageSize: 20 })
    expect(await screen.findByText("行情进程超时")).toBeInTheDocument()

    await userEvent.selectOptions(screen.getByRole("combobox", { name: "按状态筛选" }), "OPEN")
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "按严重程度筛选" }),
      "ERROR",
    )
    await userEvent.type(
      screen.getByRole("textbox", { name: "按告警类型筛选" }),
      "WORKER_TIMEOUT",
    )
    await userEvent.click(screen.getByRole("button", { name: "筛选" }))

    await waitFor(() => {
      expect(loadAlerts).toHaveBeenLastCalledWith({
        page: 1,
        pageSize: 20,
        status: "OPEN",
        severity: "ERROR",
        alertType: "WORKER_TIMEOUT",
      })
    })
  })

  it("支持空数据，并在接口失败后重试恢复", async () => {
    const loadAlerts = vi
      .fn()
      .mockRejectedValueOnce(new ApiError("暂不可用", {
        code: "ALERT_BACKEND_UNAVAILABLE",
        status: 503,
      }))
      .mockResolvedValueOnce({ items: [], total: 0, page: 1, pageSize: 20 })
    renderPage(gateway({ loadAlerts }))

    expect(await screen.findByText("系统告警读取失败")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "重新读取" }))
    expect(await screen.findByText("没有符合条件的告警")).toBeInTheDocument()
    expect(loadAlerts).toHaveBeenCalledTimes(2)
  })

  it("非法操作保持禁用，确认已知不被误称为解决", async () => {
    renderPage(gateway({
      loadAlert: vi.fn().mockResolvedValue(alert({
        allowedActions: ["ACKNOWLEDGE"],
      })),
    }))

    await userEvent.click(await screen.findByRole("button", { name: "查看详情" }))
    expect(await screen.findByText("确认已知不等于问题解决；只有问题已处理完成后才能人工解决。"))
      .toBeInTheDocument()
    expect(screen.getByRole("button", { name: "确认已知" })).toBeEnabled()
    expect(screen.getByRole("button", { name: "人工解决" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "提交重试" })).toBeDisabled()
  })

  it("操作期间防止重复提交，成功后刷新列表和详情", async () => {
    let finish: ((value: { alert: AlertItem; jobId: string | null }) => void) | undefined
    const runAction = vi.fn().mockReturnValue(new Promise((resolve) => {
      finish = resolve
    }))
    const loadAlerts = vi.fn().mockResolvedValue({
      items: [alert()],
      total: 1,
      page: 1,
      pageSize: 20,
    })
    const loadAlert = vi.fn().mockResolvedValue(alert())
    renderPage(gateway({ loadAlerts, loadAlert, runAction }))

    await userEvent.click(await screen.findByRole("button", { name: "查看详情" }))
    await userEvent.click(await screen.findByRole("button", { name: "确认已知" }))
    expect(screen.getByText("仅记录你已看到这条告警，不代表问题已经恢复，也不会停止后续提醒。"))
      .toBeInTheDocument()
    await userEvent.type(screen.getByRole("textbox", { name: "操作原因" }), "值班人员已知")
    const submit = screen.getByRole("button", { name: "确认已知" })
    await userEvent.click(submit)
    expect(submit).toBeDisabled()
    await userEvent.click(submit)
    expect(runAction).toHaveBeenCalledTimes(1)
    expect(runAction).toHaveBeenCalledWith({
      alertId: "alert-1",
      action: "ACKNOWLEDGE",
      expectedVersion: 3,
      reason: "值班人员已知",
    })

    finish?.({ alert: alert({ status: "ACKNOWLEDGED", version: 4 }), jobId: null })
    expect(await screen.findByText("确认已知已完成")).toBeInTheDocument()
    await waitFor(() => {
      expect(loadAlerts).toHaveBeenCalledTimes(2)
      expect(loadAlert).toHaveBeenCalledTimes(2)
    })
  })

  it("人工解决必须填写说明，并显示操作失败原因", async () => {
    const runAction = vi.fn().mockRejectedValue(new ApiError("告警版本已变化", {
      code: "ALERT_VERSION_CONFLICT",
      status: 409,
    }))
    renderPage(gateway({
      loadAlert: vi.fn().mockResolvedValue(alert({ allowedActions: ["RESOLVE"] })),
      runAction,
    }))

    await userEvent.click(await screen.findByRole("button", { name: "查看详情" }))
    await userEvent.click(await screen.findByRole("button", { name: "人工解决" }))
    const submit = screen.getByRole("button", { name: "确认解决" })
    expect(submit).toBeDisabled()
    await userEvent.type(
      screen.getByRole("textbox", { name: "处理说明" }),
      "进程已恢复并完成数据核对",
    )
    await userEvent.click(submit)

    expect(await screen.findByText("告警版本已变化")).toBeInTheDocument()
    expect(runAction).toHaveBeenCalledTimes(1)
  })
})
