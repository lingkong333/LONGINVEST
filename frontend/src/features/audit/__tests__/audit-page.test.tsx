import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { MemoryRouter } from "react-router-dom"
import { describe, expect, it, vi } from "vitest"

import { AuditPage } from "@/features/audit/audit-page"
import type {
  AuditEvent,
  AuditGateway,
  AuditPage as AuditPageResult,
} from "@/features/audit/types"

const event: AuditEvent = {
  id: "00000000-0000-4000-8000-000000000001",
  occurredAt: "2026-07-23T04:00:00Z",
  actorUserId: "user-1",
  sessionId: "session-1",
  trustedIp: "127.0.0.1",
  actionCode: "TARGET_UPDATE",
  objectType: "target",
  objectId: "target-1",
  result: "SUCCESS",
  beforeSummary: { price: "10.00", enabled: false },
  afterSummary: { price: "11.00", enabled: true },
  reason: "人工调整",
  requestId: "req-1",
  idempotencyKey: "idem-1",
  riskLevel: "HIGH",
}

function page(
  overrides: Partial<AuditPageResult> = {},
): AuditPageResult {
  return {
    items: [event],
    pagination: { page: 1, pageSize: 20, total: 1 },
    allowedActions: [],
    ...overrides,
  }
}

function gateway(overrides: Partial<AuditGateway> = {}): AuditGateway {
  return {
    loadEvents: vi.fn().mockResolvedValue(page()),
    ...overrides,
  }
}

function renderPage(api: AuditGateway, initialUrl = "/audit") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  render(
    <MemoryRouter initialEntries={[initialUrl]}>
      <QueryClientProvider client={client}>
        <AuditPage gateway={api} />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

describe("审计记录页面", () => {
  it("等待响应时显示加载状态", () => {
    renderPage(gateway({
      loadEvents: vi.fn().mockReturnValue(new Promise(() => undefined)),
    }))

    expect(screen.getByText("正在读取审计记录")).toBeInTheDocument()
  })

  it("展示只读记录和安全的前后变更详情", async () => {
    renderPage(gateway())

    expect(await screen.findByText("TARGET_UPDATE")).toBeInTheDocument()
    expect(screen.getByText("只读 · 无可用操作")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", {
      name: "查看 TARGET_UPDATE 审计详情",
    }))

    const dialog = screen.getByRole("dialog")
    expect(within(dialog).getByText("人工调整")).toBeInTheDocument()
    expect(within(dialog).getByText("10.00")).toBeInTheDocument()
    expect(within(dialog).getByText("11.00")).toBeInTheDocument()
    expect(within(dialog).getByText("req-1")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /修改|删除|重放/ }))
      .not.toBeInTheDocument()
  })

  it("空结果显示明确空状态", async () => {
    renderPage(gateway({
      loadEvents: vi.fn().mockResolvedValue(page({
        items: [],
        pagination: { page: 1, pageSize: 20, total: 0 },
      })),
    }))

    expect(await screen.findByText("没有符合条件的审计记录"))
      .toBeInTheDocument()
  })

  it("应用筛选后从第一页向服务器重新查询", async () => {
    const api = gateway()
    renderPage(api)
    await screen.findByText("TARGET_UPDATE")

    await userEvent.type(screen.getByLabelText("用户标识"), "user-2")
    await userEvent.type(screen.getByLabelText("操作代码"), "SESSION_REVOKE")
    await userEvent.type(screen.getByLabelText("对象类型"), "session")
    await userEvent.type(screen.getByLabelText("操作结果"), "SUCCESS")
    await userEvent.type(screen.getByLabelText("风险等级"), "HIGH")
    await userEvent.click(screen.getByRole("button", { name: "应用筛选" }))

    expect(api.loadEvents).toHaveBeenLastCalledWith(expect.objectContaining({
      page: 1,
      pageSize: 20,
      actorUserId: "user-2",
      actionCode: "SESSION_REVOKE",
      objectType: "session",
      result: "SUCCESS",
      riskLevel: "HIGH",
    }))
  })

  it("分页操作保留筛选并读取下一页", async () => {
    const api = gateway({
      loadEvents: vi.fn().mockResolvedValue(page({
        pagination: { page: 1, pageSize: 20, total: 45 },
      })),
    })
    renderPage(api, "/audit?action_code=TARGET_UPDATE")
    await screen.findByText("TARGET_UPDATE")
    await userEvent.click(screen.getByRole("button", { name: "下一页" }))

    expect(api.loadEvents).toHaveBeenLastCalledWith(expect.objectContaining({
      page: 2,
      actionCode: "TARGET_UPDATE",
    }))
  })

  it("接口失败时显示诊断和重试入口", async () => {
    renderPage(gateway({
      loadEvents: vi.fn().mockRejectedValue(new Error("audit failed")),
    }))

    expect(await screen.findByText("审计记录暂时无法读取"))
      .toBeInTheDocument()
    expect(screen.getByText("UNKNOWN_ERROR")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "重新加载" }))
      .toBeInTheDocument()
  })

  it("服务器返回任何操作权限时拒绝展示记录", async () => {
    renderPage(gateway({
      loadEvents: vi.fn().mockResolvedValue(page({
        allowedActions: ["DELETE"],
      })),
    }))

    expect(await screen.findByText("审计权限响应异常")).toBeInTheDocument()
    expect(screen.getByText("AUDIT_ACTIONS_NOT_ALLOWED")).toBeInTheDocument()
    expect(screen.queryByText("TARGET_UPDATE")).not.toBeInTheDocument()
  })
})
