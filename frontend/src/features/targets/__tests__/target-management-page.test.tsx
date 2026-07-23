import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import { TargetManagementPage } from "@/features/targets"
import type {
  TargetItem,
  TargetManagementApi,
} from "@/features/targets/types"

const target: TargetItem = {
  subscription_id: "subscription-1",
  revision_id: "revision-1",
  revision_no: 2,
  binding_version: 3,
  values: {
    low_strong: "8.00",
    low_watch: "9.00",
    high_watch: "12.00",
    high_strong: "13.00",
  },
  source: "MANUAL",
  status: "READY",
  target_date: "2026-08-01",
  strategy_version_id: null,
  parameter_snapshot: {},
  data_version: null,
  source_code_hash: null,
  content_hash: "a".repeat(64),
  activated_at: "2026-07-23T02:00:00Z",
  allowed_actions: ["MANUAL_EDIT"],
  allowedActions: ["MANUAL_EDIT"],
}

function api(): TargetManagementApi {
  return {
    listTargets: vi.fn().mockResolvedValue([target]),
    getTarget: vi.fn().mockResolvedValue(target),
    listHistory: vi.fn().mockResolvedValue([]),
    listRuns: vi.fn().mockResolvedValue([]),
    listReviews: vi.fn().mockResolvedValue([]),
    setManual: vi.fn().mockResolvedValue(undefined),
    calculate: vi.fn().mockResolvedValue(undefined),
    retry: vi.fn().mockResolvedValue(undefined),
    restore: vi.fn().mockResolvedValue(undefined),
    approve: vi.fn().mockResolvedValue(undefined),
    reject: vi.fn().mockResolvedValue(undefined),
    recalculate: vi.fn().mockResolvedValue(undefined),
  }
}

function renderPage(targetApi: TargetManagementApi) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={client}>
      <TargetManagementPage api={targetApi} />
    </QueryClientProvider>,
  )
}

describe("目标管理页面", () => {
  it("展示四档目标，并严格按后端允许操作控制按钮", async () => {
    renderPage(api())

    expect(await screen.findByRole("heading", { name: "目标管理" })).toBeInTheDocument()
    expect(screen.getByText("¥ 8.00 — 13.00")).toBeInTheDocument()

    await userEvent.click(screen.getByRole("button", { name: /subscrip/ }))

    expect(screen.getByRole("button", { name: "手工编辑" })).toBeEnabled()
    expect(screen.getByRole("button", { name: "运行计算" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "重试计算" })).toBeDisabled()
  })
})
