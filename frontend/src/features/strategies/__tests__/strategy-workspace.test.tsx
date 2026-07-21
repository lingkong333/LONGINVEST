import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, fireEvent, render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import { StrategyBacktestWorkspace, StrategyWorkspace } from "@/features/strategies"
import type { StrategyApi, StrategyDraft } from "@/features/strategies"

vi.mock("@monaco-editor/react", () => ({
  default: ({ value, onChange, language, options }: {
    value: string
    onChange: (next: string | undefined) => void
    language: string
    options: { lineNumbers: string; find: { addExtraSpaceOnTop: boolean }; bracketPairColorization: { enabled: boolean } }
  }) => (
    <textarea
      aria-label="Python source"
      data-bracket-colorization={String(options.bracketPairColorization.enabled)}
      data-find-widget={String(options.find.addExtraSpaceOnTop)}
      data-language={language}
      data-line-numbers={options.lineNumbers}
      value={value}
      onChange={(event) => onChange(event.target.value)}
    />
  ),
}))

const draft: StrategyDraft = {
  id: "draft-1",
  strategyId: "strategy-1",
  name: "Long hold",
  description: "Monthly trend strategy",
  sourceCode: "def calculate_targets(history, params, context):\n    return [1, 2, 3, 4]",
  parameterSchema: '{"type":"object","properties":{}}',
  version: 4,
  updatedAt: "2026-07-21T09:00:00Z",
}

function createApi(overrides: Partial<StrategyApi> = {}): StrategyApi {
  return {
    getDraft: vi.fn().mockResolvedValue(draft),
    saveDraft: vi.fn().mockResolvedValue({ ...draft, version: 5 }),
    listRevisions: vi.fn().mockResolvedValue([]),
    restoreRevision: vi.fn(),
    validateDraft: vi.fn(),
    testDraft: vi.fn(),
    publishDraft: vi.fn(),
    archiveStrategy: vi.fn(),
    listVersions: vi.fn().mockResolvedValue([]),
    createHoldoutBacktest: vi.fn(),
    getHoldoutBacktest: vi.fn(),
    ...overrides,
  }
}

function renderWorkspace(ui: React.ReactElement) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>)
}

describe("StrategyWorkspace", () => {
  afterEach(() => vi.useRealTimers())

  it("uses Monaco with Python, line numbers, find widget, and bracket coloring without browser persistence", async () => {
    const api = createApi()
    const storage = vi.spyOn(Storage.prototype, "setItem")
    renderWorkspace(<StrategyWorkspace strategyId="strategy-1" api={api} />)

    const editor = await screen.findByRole("textbox", { name: "Python source" })
    expect(editor).toHaveAttribute("data-language", "python")
    expect(editor).toHaveAttribute("data-line-numbers", "on")
    expect(editor).toHaveAttribute("data-find-widget", "true")
    expect(editor).toHaveAttribute("data-bracket-colorization", "true")
    expect(storage).not.toHaveBeenCalled()
  })

  it("auto-saves changed drafts after 30 seconds with the current expected version", async () => {
    const api = createApi()
    renderWorkspace(<StrategyWorkspace strategyId="strategy-1" api={api} />)
    const editor = await screen.findByRole("textbox", { name: "Python source" })
    vi.useFakeTimers()
    fireEvent.change(editor, { target: { value: `${draft.sourceCode}\n# changed` } })
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })

    expect(api.saveDraft).toHaveBeenCalledWith(
      "strategy-1",
      expect.objectContaining({ expectedVersion: 4, sourceCode: expect.stringContaining("# changed") }),
    )
  })

  it("stops automatic saving after a conflict and offers copy, discard, or merge retry", async () => {
    const api = createApi({
      saveDraft: vi.fn().mockRejectedValue({ status: 409, current: { ...draft, version: 8, sourceCode: "server code" } }),
    })
    renderWorkspace(<StrategyWorkspace strategyId="strategy-1" api={api} />)
    const editor = await screen.findByRole("textbox", { name: "Python source" })
    vi.useFakeTimers()
    fireEvent.change(editor, { target: { value: `${draft.sourceCode}\n# local` } })
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })

    expect(screen.getByRole("dialog", { name: "Save conflict" })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Copy local source" })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Discard local changes" })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Merge and retry" })).toBeInTheDocument()
  })

  it("reports successful validation after a confirmed action", async () => {
    const api = createApi()
    const user = userEvent.setup()
    renderWorkspace(<StrategyWorkspace strategyId="strategy-1" api={api} />)
    await screen.findByRole("textbox", { name: "Python source" })

    await user.click(screen.getByRole("button", { name: "Validate" }))
    await user.type(screen.getByRole("textbox", { name: "Reason" }), "Check before publishing")
    await user.click(screen.getByRole("button", { name: "Confirm" }))

    expect(await screen.findByRole("status")).toHaveTextContent("Validation requested")
    expect(api.validateDraft).toHaveBeenCalledWith("strategy-1", "Check before publishing")
  })
})

describe("StrategyBacktestWorkspace", () => {
  it("requires all four manual dates and makes training and test isolation visible", async () => {
    const api = createApi()
    const user = userEvent.setup()
    renderWorkspace(<StrategyBacktestWorkspace strategyId="strategy-1" api={api} />)

    expect(screen.getByText("The strategy receives training data only. Test-period data stays outside the sandbox.")).toBeInTheDocument()
    await user.click(screen.getByRole("button", { name: "Run holdout backtest" }))

    expect(screen.getByText("Training start date is required")).toBeInTheDocument()
    expect(api.createHoldoutBacktest).not.toHaveBeenCalled()
  })
})
