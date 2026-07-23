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
  NotificationsPage,
  type NotificationGateway,
} from "@/features/notifications"

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

function gateway(
  overrides: Partial<NotificationGateway> = {},
): NotificationGateway {
  return {
    loadEvents: vi.fn().mockResolvedValue({
      items: [{
        id: "event-1",
        eventType: "signal.high",
        businessEventType: "signal.transitioned",
        businessObjectType: "subscription",
        businessObjectId: "subscription-1",
        severity: "INFO",
        status: "PARTIAL",
        eligibilityStatus: "ELIGIBLE",
        suppressionReason: null,
        effectiveChannels: ["WECOM", "EMAIL"],
        templateVersion: "v1",
        createdAt: "2026-07-23T01:00:00Z",
        allowedActions: [],
      }],
      page: 1,
      pageSize: 100,
      total: 1,
    }),
    loadDeliveries: vi.fn().mockResolvedValue({
      items: [{
        id: "delivery-1",
        eventId: "event-1",
        generation: 1,
        channel: "WECOM",
        targetFingerprint: "sha256:target",
        status: "SENT",
        attemptCount: 1,
        nextRetryAt: null,
        sentAt: "2026-07-23T01:01:00Z",
        errorCode: null,
        createdAt: "2026-07-23T01:00:00Z",
        updatedAt: "2026-07-23T01:01:00Z",
        allowedActions: ["RETRY"],
        requiresDuplicateConfirmation: true,
      }],
      page: 1,
      pageSize: 100,
      total: 1,
    }),
    loadAttempts: vi.fn().mockResolvedValue({
      items: [],
      page: 1,
      pageSize: 100,
      total: 0,
    }),
    retryDelivery: vi.fn().mockResolvedValue(undefined),
    cancelDelivery: vi.fn().mockResolvedValue(undefined),
    loadChannels: vi.fn().mockResolvedValue([
      {
        channel: "WECOM",
        enabled: true,
        timeoutSeconds: 5,
        smtpHost: null,
        smtpPort: null,
        security: null,
        username: null,
        sender: null,
        recipients: [],
        version: 2,
        secretConfigured: true,
        secretFingerprint: "sensitive-fingerprint",
        circuitState: "CLOSED",
        circuitFailures: 0,
        circuitRetryAt: null,
        allowedActions: [],
      },
      {
        channel: "EMAIL",
        enabled: false,
        timeoutSeconds: 10,
        smtpHost: "smtp.example.com",
        smtpPort: 465,
        security: "SSL",
        username: "mailer",
        sender: "sender@example.com",
        recipients: ["secret-recipient@example.com"],
        version: 1,
        secretConfigured: false,
        secretFingerprint: null,
        circuitState: "OPEN",
        circuitFailures: 3,
        circuitRetryAt: "2026-07-23T01:30:00Z",
        allowedActions: [],
      },
    ]),
    updateChannel: vi.fn().mockResolvedValue(undefined),
    runChannelAction: vi.fn().mockResolvedValue(undefined),
    loadPolicy: vi.fn().mockImplementation(async (scope) => ({
      scope,
      enabled: true,
      channels: [],
      warning: [],
      error: [],
      critical: [],
      recovered: [],
      dailyUnresolved: [],
      version: 1,
      allowedActions: [],
    })),
    updatePolicy: vi.fn().mockResolvedValue(undefined),
    loadTemplates: vi.fn().mockResolvedValue([]),
    previewTemplate: vi.fn(),
    activateTemplate: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  }
}

function renderPage(notificationApi: NotificationGateway) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider gateway={authGateway}>
        <NotificationsPage gateway={notificationApi} />
      </AuthProvider>
    </QueryClientProvider>,
  )
}

describe("通知中心", () => {
  it("展示冻结渠道和渠道级投递结果", async () => {
    renderPage(gateway())

    expect(await screen.findByText("signal.high")).toBeInTheDocument()
    expect(screen.getByText("企业微信、邮件")).toBeInTheDocument()
    expect(screen.getByText("部分成功")).toBeInTheDocument()
  })

  it("没有事件时显示明确空状态", async () => {
    renderPage(gateway({
      loadEvents: vi.fn().mockResolvedValue({
        items: [],
        page: 1,
        pageSize: 100,
        total: 0,
      }),
    }))

    expect(await screen.findByText("暂无通知事件")).toBeInTheDocument()
  })

  it("SENT 重发必须填写原因并确认重复风险", async () => {
    let finish: (() => void) | undefined
    const retryDelivery = vi.fn().mockImplementation(
      () => new Promise<void>((resolve) => { finish = resolve }),
    )
    renderPage(gateway({ retryDelivery }))

    await userEvent.click(screen.getByRole("button", { name: "渠道投递" }))
    await userEvent.click(
      await screen.findByRole("button", { name: "重试 企业微信 投递" }),
    )
    const dialog = screen.getByRole("dialog")
    const submit = within(dialog).getByRole("button", { name: "确认执行" })
    expect(submit).toBeDisabled()

    await userEvent.type(
      within(dialog).getByLabelText("操作原因"),
      "人工核对后重发",
    )
    expect(submit).toBeDisabled()
    await userEvent.click(within(dialog).getByRole("checkbox"))
    await userEvent.click(submit)

    expect(retryDelivery).toHaveBeenCalledWith({
      deliveryId: "delivery-1",
      reason: "人工核对后重发",
      confirmDuplicateRisk: true,
    })
    expect(submit).toBeDisabled()
    finish?.()
  })

  it("后端未返回允许动作时渠道操作保持禁用且不回显敏感值", async () => {
    renderPage(gateway())

    await userEvent.click(screen.getByRole("button", { name: "通知渠道" }))
    expect(await screen.findByText("企业微信")).toBeInTheDocument()
    expect(screen.getAllByRole("button", { name: "发送测试" })[0])
      .toBeDisabled()
    expect(screen.getByText("1 个已配置地址")).toBeInTheDocument()
    expect(screen.queryByText("sensitive-fingerprint")).not.toBeInTheDocument()
    expect(screen.queryByText("secret-recipient@example.com"))
      .not.toBeInTheDocument()
  })

  it("渠道配置按许可保存且提交期间阻止重复提交", async () => {
    let finish: (() => void) | undefined
    const updateChannel = vi.fn().mockImplementation(
      () => new Promise<void>((resolve) => { finish = resolve }),
    )
    const notificationApi = gateway({ updateChannel })
    vi.mocked(notificationApi.loadChannels).mockResolvedValue([
      {
        ...(await gateway().loadChannels())[0],
        allowedActions: ["UPDATE"],
      },
    ])
    renderPage(notificationApi)

    await userEvent.click(screen.getByRole("button", { name: "通知渠道" }))
    await userEvent.click(
      await screen.findByRole("button", { name: "编辑配置" }),
    )
    const dialog = screen.getByRole("dialog")
    await userEvent.clear(within(dialog).getByLabelText("连接超时（秒）"))
    await userEvent.type(within(dialog).getByLabelText("连接超时（秒）"), "8")
    await userEvent.type(within(dialog).getByLabelText("修改原因"), "调整超时")
    const submit = within(dialog).getByRole("button", { name: "保存配置" })
    await userEvent.click(submit)
    await userEvent.click(submit)

    expect(updateChannel).toHaveBeenCalledTimes(1)
    expect(updateChannel).toHaveBeenCalledWith(
      expect.objectContaining({ channel: "WECOM", timeoutSeconds: 8, version: 2 }),
      "调整超时",
    )
    expect(submit).toBeDisabled()
    finish?.()
  })

  it("渠道版本冲突后保留用户输入供再次处理", async () => {
    const updateChannel = vi.fn().mockRejectedValue(
      new Error("配置已被其他操作更新"),
    )
    const notificationApi = gateway({ updateChannel })
    vi.mocked(notificationApi.loadChannels).mockResolvedValue([
      {
        ...(await gateway().loadChannels())[0],
        allowedActions: ["UPDATE"],
      },
    ])
    renderPage(notificationApi)

    await userEvent.click(screen.getByRole("button", { name: "通知渠道" }))
    await userEvent.click(
      await screen.findByRole("button", { name: "编辑配置" }),
    )
    const dialog = screen.getByRole("dialog")
    await userEvent.clear(within(dialog).getByLabelText("连接超时（秒）"))
    await userEvent.type(within(dialog).getByLabelText("连接超时（秒）"), "9")
    await userEvent.type(within(dialog).getByLabelText("修改原因"), "调整超时")
    await userEvent.click(
      within(dialog).getByRole("button", { name: "保存配置" }),
    )

    expect(await within(dialog).findByText("配置已被其他操作更新"))
      .toBeInTheDocument()
    expect(within(dialog).getByLabelText("连接超时（秒）")).toHaveValue(9)
    expect(within(dialog).getByLabelText("修改原因")).toHaveValue("调整超时")
  })

  it("发送尝试读取失败只影响详情区", async () => {
    renderPage(gateway({
      loadAttempts: vi.fn().mockRejectedValue(new Error("attempt failed")),
    }))

    await userEvent.click(screen.getByRole("button", { name: "渠道投递" }))
    await userEvent.click(
      await screen.findByRole("button", { name: "查看 企业微信 尝试" }),
    )

    expect(await screen.findByText("尝试记录读取失败，不影响投递列表。"))
      .toBeInTheDocument()
    expect(screen.getByText("发送成功")).toBeInTheDocument()
  })

  it("一个策略读取失败不影响其他策略", async () => {
    renderPage(gateway({
      loadPolicy: vi.fn().mockImplementation(async (scope) => {
        if (scope === "global") throw new Error("global policy failed")
        return {
          scope,
          enabled: true,
          channels: [],
          warning: [],
          error: [],
          critical: [],
          recovered: [],
          dailyUnresolved: [],
          version: 1,
          allowedActions: [],
        }
      }),
    }))

    await userEvent.click(screen.getByRole("button", { name: "通知策略" }))

    expect(await screen.findByText("该策略暂时无法读取，其他策略不受影响。"))
      .toBeInTheDocument()
    expect(screen.getByRole("heading", { name: "股票信号" }))
      .toBeInTheDocument()
    expect(screen.getByRole("heading", { name: "系统告警" }))
      .toBeInTheDocument()
  })
})
