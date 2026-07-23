import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import {
  AuthProvider,
  type AuthGateway,
  type AuthState,
} from "@/features/auth"
import {
  type SecretStatus,
  type SettingItem,
  type SettingsGateway,
  SettingsPage,
} from "@/features/settings"
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

const definition = {
  valueType: "object",
  defaultValue: {},
  valueSchema: { type: "object" },
  sensitive: false,
  appliesToNewTasks: true,
  rollbackAllowed: true,
}

function setting(overrides: Partial<SettingItem> = {}): SettingItem {
  return {
    key: "notification.channel.wecom",
    value: { enabled: true, timeout_seconds: 5 },
    schemaVersion: 1,
    version: 3,
    description: "企业微信机器人运行参数",
    updatedBy: "user-1",
    updatedAt: "2026-07-23T02:00:00Z",
    definition,
    allowedActions: ["UPDATE", "ROLLBACK"],
    ...overrides,
  }
}

function secret(overrides: Partial<SecretStatus> = {}): SecretStatus {
  return {
    key: "notification.wecom.webhook",
    configured: true,
    masked: "********",
    version: 2,
    fingerprint: "abc123",
    updatedAt: "2026-07-23T02:00:00Z",
    definition: {
      ...definition,
      sensitive: true,
      rollbackAllowed: false,
    },
    allowedActions: ["UPDATE", "CLEAR"],
    ...overrides,
  }
}

function gateway(overrides: Partial<SettingsGateway> = {}): SettingsGateway {
  return {
    loadOverview: vi.fn().mockResolvedValue({
      settings: [setting()],
      secrets: [secret()],
    }),
    loadHistory: vi.fn().mockResolvedValue([{
      version: 2,
      value: { enabled: false, timeout_seconds: 5 },
      reason: "临时停用",
      actorUserId: "user-1",
      requestId: "req-history",
      createdAt: "2026-07-22T02:00:00Z",
      allowedActions: ["ROLLBACK"],
    }]),
    updateSetting: vi.fn().mockResolvedValue(setting({ version: 4 })),
    rollbackSetting: vi.fn().mockResolvedValue(setting({ version: 4 })),
    updateSecret: vi.fn().mockResolvedValue(secret({ version: 3 })),
    ...overrides,
  }
}

function renderPage(value: SettingsGateway) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider gateway={authGateway}>
        <SettingsPage gateway={value} />
      </AuthProvider>
    </QueryClientProvider>,
  )
}

describe("系统设置页面", () => {
  it("显示空数据，并在读取失败后重试恢复", async () => {
    const loadOverview = vi
      .fn()
      .mockRejectedValueOnce(new ApiError("无权限", {
        code: "SETTINGS_FORBIDDEN",
        status: 403,
      }))
      .mockResolvedValueOnce({ settings: [], secrets: [] })
    renderPage(gateway({ loadOverview }))

    expect(await screen.findByText("系统设置读取失败")).toBeInTheDocument()
    expect(screen.getByText("SETTINGS_FORBIDDEN")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "重新读取" }))
    expect(await screen.findByText("没有可管理的设置")).toBeInTheDocument()
  })

  it("无允许操作时保持只读，非法范围不会打开提交确认", async () => {
    const noPermission = setting({ allowedActions: [] })
    const value = gateway({
      loadOverview: vi.fn().mockResolvedValue({
        settings: [noPermission],
        secrets: [],
      }),
    })
    renderPage(value)

    expect(await screen.findByRole("button", { name: "保存配置" })).toBeDisabled()
    expect(screen.getByRole("spinbutton", { name: "请求超时（1 到 15 秒）" }))
      .toBeDisabled()
  })

  it("拒绝超出白名单范围的值，并保留表单输入", async () => {
    renderPage(gateway())

    const timeout = await screen.findByRole("spinbutton", {
      name: "请求超时（1 到 15 秒）",
    })
    await userEvent.clear(timeout)
    await userEvent.type(timeout, "20")
    await userEvent.click(screen.getByRole("button", { name: "保存配置" }))

    expect(screen.getByText("配置内容不符合允许的类型或范围，请检查后再保存。"))
      .toBeInTheDocument()
    expect(timeout).toHaveValue(20)
    expect(screen.queryByText("确认保存配置")).not.toBeInTheDocument()
  })

  it("保存期间防重复，失败后表单和变更原因保持不变", async () => {
    let reject: ((reason: unknown) => void) | undefined
    const updateSetting = vi.fn().mockReturnValue(new Promise((_, rejectPromise) => {
      reject = rejectPromise
    }))
    renderPage(gateway({ updateSetting }))

    const timeout = await screen.findByRole("spinbutton", {
      name: "请求超时（1 到 15 秒）",
    })
    await userEvent.clear(timeout)
    await userEvent.type(timeout, "8")
    await userEvent.click(screen.getByRole("button", { name: "保存配置" }))
    await userEvent.type(screen.getByRole("textbox", { name: "变更原因" }), "调整连接超时")
    const submit = screen.getByRole("button", { name: "确认执行" })
    await userEvent.click(submit)
    expect(submit).toBeDisabled()
    await userEvent.click(submit)
    expect(updateSetting).toHaveBeenCalledTimes(1)

    reject?.(new ApiError("配置版本已变化", {
      code: "SETTING_VERSION_CONFLICT",
      status: 409,
    }))
    expect(await screen.findByText("配置版本已变化")).toBeInTheDocument()
    expect(screen.getByRole("textbox", { name: "变更原因" }))
      .toHaveValue("调整连接超时")
    expect(timeout).toHaveValue(8)
  })

  it("历史回滚只在两级允许动作同时存在时可用", async () => {
    renderPage(gateway())

    const rollback = await screen.findByRole("button", { name: "回滚到此版本" })
    expect(rollback).toBeEnabled()
    await userEvent.click(rollback)
    expect(screen.getByText("回滚会复制历史值并创建一个新版本，不会修改历史记录。"))
      .toBeInTheDocument()
  })

  it("敏感值只显示掩码，留空保留，清空必须显式选择", async () => {
    const updateSecret = vi.fn().mockResolvedValue(secret({ configured: false }))
    renderPage(gateway({ updateSecret }))

    await userEvent.click(await screen.findByRole("tab", { name: "敏感配置" }))
    expect(screen.getByText("当前状态：已配置 ********")).toBeInTheDocument()
    const secretInput = screen.getByLabelText("新值")
    expect(secretInput).toHaveValue("")
    await userEvent.click(screen.getByRole("button", { name: "保存敏感配置" }))
    expect(screen.getByText("输入留空，当前敏感值将保持不变。")).toBeInTheDocument()
    await userEvent.type(
      screen.getByRole("textbox", { name: "敏感配置变更原因" }),
      "确认保留原配置",
    )
    await userEvent.click(screen.getByRole("button", { name: "确认执行" }))
    await waitFor(() => {
      expect(updateSecret).toHaveBeenCalledWith({
        key: "notification.wecom.webhook",
        value: null,
        clearSecret: false,
        expectedVersion: 2,
        reason: "确认保留原配置",
      })
    })

    await userEvent.click(screen.getByLabelText("明确清空当前敏感值"))
    await userEvent.click(screen.getByRole("button", { name: "清空敏感值" }))
    expect(screen.getByText("清空后相关通知渠道可能无法工作，此操作会被审计。"))
      .toBeInTheDocument()
  })
})
