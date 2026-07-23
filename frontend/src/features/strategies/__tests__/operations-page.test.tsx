import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import { StrategyOperationsPage } from "@/features/strategies"
import type { StrategyApi, StrategyEditorComponents } from "@/features/strategies"

const editorComponents: StrategyEditorComponents = {
  CodeEditor: ({ value, onChange, ariaLabel }) => <textarea aria-label={ariaLabel} value={value} onChange={(event) => onChange(event.target.value)} />,
  DiffViewer: () => null,
}

function createApi(overrides: Partial<StrategyApi> = {}): StrategyApi {
  return {
    listStrategies: vi.fn().mockResolvedValue({ items: [], canCreate: false }),
    createStrategy: vi.fn(),
    getDraft: vi.fn().mockResolvedValue({
      id: "draft-1",
      strategyId: "strategy-1",
      name: "长期策略",
      description: "说明",
      sourceCode: "pass",
      parameterSchema: "{}",
      version: 1,
      strategyVersion: 1,
      updatedAt: "",
      allowedActions: [],
      canSave: true,
      canRestoreRevision: true,
    }),
    saveDraft: vi.fn(),
    listRevisions: vi.fn().mockResolvedValue([]),
    restoreRevision: vi.fn(),
    validateDraft: vi.fn(),
    testDraft: vi.fn(),
    publishDraft: vi.fn(),
    archiveStrategy: vi.fn(),
    listVersions: vi.fn().mockResolvedValue([]),
    createHoldoutBacktest: vi.fn(),
    listHoldoutBacktests: vi.fn().mockResolvedValue({ items: [], page: 1, pageSize: 200, total: 0 }),
    getHoldoutBacktest: vi.fn(),
    getHoldoutBacktestSummary: vi.fn(),
    controlHoldoutBacktest: vi.fn(),
    ...overrides,
  }
}

function renderPage(api: StrategyApi) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={queryClient}><StrategyOperationsPage api={api} editorComponents={editorComponents} /></QueryClientProvider>)
}

describe("策略总览", () => {
  it("空列表明确显示空态，且不越过服务端权限开放创建", async () => {
    renderPage(createApi())

    expect(await screen.findByText("暂无策略")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "新建策略" })).not.toBeInTheDocument()
  })

  it("服务端允许时可填写名称和原因，提交期间防止重复创建", async () => {
    const createStrategy = vi.fn().mockReturnValue(new Promise(() => undefined))
    const user = userEvent.setup()
    renderPage(createApi({
      listStrategies: vi.fn().mockResolvedValue({ items: [], canCreate: true }),
      createStrategy,
    }))

    await user.click(await screen.findByRole("button", { name: "新建策略" }))
    await user.type(screen.getByRole("textbox", { name: "策略名称" }), "新策略")
    await user.type(screen.getByRole("textbox", { name: "创建原因" }), "验证新思路")
    await user.click(screen.getByRole("button", { name: "确认创建" }))

    expect(createStrategy).toHaveBeenCalledWith("新策略", "验证新思路")
    expect(screen.getByRole("button", { name: "创建中" })).toBeDisabled()
  })

  it("选择策略后可在编辑和回测视图切换", async () => {
    const user = userEvent.setup()
    renderPage(createApi({
      listStrategies: vi.fn().mockResolvedValue({
        items: [{ id: "strategy-1", name: "长期策略", status: "DRAFT" }],
        canCreate: false,
      }),
    }))

    expect(await screen.findByRole("textbox", { name: "Python 策略源码" })).toBeInTheDocument()
    await user.click(screen.getByRole("tab", { name: "回测" }))

    expect(await screen.findByRole("heading", { name: "样本外回测" })).toBeInTheDocument()
  })
})
