import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import {
  CalendarPage,
  type CalendarGateway,
  type CalendarSnapshot,
} from "@/features/calendar"
import { ApiError } from "@/shared/api/client"

const snapshot: CalendarSnapshot = {
  days: [{
    tradeDate: "2026-07-23",
    isTradingDay: true,
    status: "CONFIRMED",
    source: "SSE_OFFICIAL",
    note: null,
    overrideReason: null,
    sessions: [
      { startsAt: "09:30:00", endsAt: "11:30:00" },
      { startsAt: "13:00:00", endsAt: "15:00:00" },
    ],
    allowedActions: ["OVERRIDE"],
  }],
  coverage: {
    market: "CN_A",
    fromDate: "2026-07-01",
    confirmedThrough: "2026-12-31",
    futureConfirmedDays: 161,
    level: "OK",
    currentVersionId: "version-3",
    missingToday: false,
    allowedActions: ["IMPORT", "OVERRIDE"],
  },
  versions: [
    {
      id: "version-3",
      market: "CN_A",
      versionNumber: 3,
      source: "SSE_OFFICIAL",
      sourceVersion: "2026",
      basedOnVersionId: null,
      reason: "年度正式日历",
      createdAt: "2026-01-01T00:00:00Z",
      isCurrent: true,
      allowedActions: [],
    },
    {
      id: "version-2",
      market: "CN_A",
      versionNumber: 2,
      source: "SSE_OFFICIAL",
      sourceVersion: "2025",
      basedOnVersionId: null,
      reason: "上一版日历",
      createdAt: "2025-01-01T00:00:00Z",
      isCurrent: false,
      allowedActions: ["RESTORE"],
    },
  ],
  allowedActions: ["IMPORT", "OVERRIDE"],
}

function gateway(overrides: Partial<CalendarGateway> = {}): CalendarGateway {
  return {
    loadSnapshot: vi.fn().mockResolvedValue(snapshot),
    overrideDay: vi.fn().mockResolvedValue(undefined),
    importCalendar: vi.fn().mockResolvedValue(undefined),
    restoreVersion: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  }
}

function renderPage(api: CalendarGateway) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <CalendarPage gateway={api} />
    </QueryClientProvider>,
  )
}

describe("交易日历页", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.setSystemTime(new Date(2026, 6, 23, 12))
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it("展示月历、覆盖状态、特殊时段和不可变版本", async () => {
    renderPage(gateway())

    expect(await screen.findByRole("heading", { name: "交易日历" })).toBeInTheDocument()
    expect(screen.getByText("2026-12-31")).toBeInTheDocument()
    expect(screen.getByText("161 天")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "2026-07-23 交易日" })).toBeEnabled()
    expect(screen.getByText("年度正式日历")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "恢复版本 v3" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "恢复版本 v2" })).toBeEnabled()
  })

  it("后端没有许可时导入和单日覆盖不能提交", async () => {
    renderPage(gateway({
      loadSnapshot: vi.fn().mockResolvedValue({
        ...snapshot,
        allowedActions: [],
        days: [{ ...snapshot.days[0], allowedActions: [] }],
      }),
    }))

    expect(await screen.findByRole("button", { name: "导入日历" })).toBeDisabled()
    await userEvent.click(screen.getByRole("button", { name: "2026-07-23 交易日" }))
    const dialog = screen.getByRole("dialog")
    await userEvent.type(within(dialog).getByLabelText("操作原因"), "临时休市")
    await userEvent.click(within(dialog).getByRole("checkbox"))
    expect(within(dialog).getByRole("button", { name: "确认覆盖" })).toBeDisabled()
  })

  it("单日覆盖要求原因和确认，并阻止重复提交", async () => {
    let finish: (() => void) | undefined
    const overrideDay = vi.fn(() => new Promise<void>((resolve) => { finish = resolve }))
    const api = gateway({ overrideDay })
    renderPage(api)

    await userEvent.click(await screen.findByRole("button", { name: "2026-07-23 交易日" }))
    const dialog = screen.getByRole("dialog")
    const submit = within(dialog).getByRole("button", { name: "确认覆盖" })
    expect(submit).toBeDisabled()
    await userEvent.click(within(dialog).getByRole("button", { name: "设为休市日" }))
    await userEvent.type(within(dialog).getByLabelText("操作原因"), "台风临时休市")
    await userEvent.click(within(dialog).getByRole("checkbox"))
    await userEvent.click(submit)
    await userEvent.click(submit)

    expect(overrideDay).toHaveBeenCalledTimes(1)
    expect(overrideDay).toHaveBeenCalledWith(expect.objectContaining({
      isTradingDay: false,
      expectedCurrentVersion: 3,
      reason: "台风临时休市",
    }))
    expect(within(dialog).getByRole("button", { name: "正在保存" })).toBeDisabled()
    finish?.()
  })

  it("恢复旧版本保留操作原因，失败后可重试", async () => {
    const restoreVersion = vi.fn().mockRejectedValue(new Error("版本已经变化"))
    renderPage(gateway({ restoreVersion }))

    await userEvent.click(await screen.findByRole("button", { name: "恢复版本 v2" }))
    const dialog = screen.getByRole("dialog")
    await userEvent.type(within(dialog).getByLabelText("操作原因"), "恢复节假日安排")
    await userEvent.click(within(dialog).getByRole("checkbox"))
    await userEvent.click(within(dialog).getByRole("button", { name: "确认恢复" }))

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("版本已经变化")
    expect(within(dialog).getByLabelText("操作原因")).toHaveValue("恢复节假日安排")
  })

  it("接口失败时显示稳定错误并允许重新加载", async () => {
    renderPage(gateway({
      loadSnapshot: vi.fn().mockRejectedValue(new ApiError("服务不可用", {
        code: "CALENDAR_BACKEND_UNAVAILABLE",
      })),
    }))

    expect(await screen.findByText("交易日历暂时无法读取")).toBeInTheDocument()
    expect(screen.getByText("错误代码：CALENDAR_BACKEND_UNAVAILABLE")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "重新加载" })).toBeEnabled()
  })
})
