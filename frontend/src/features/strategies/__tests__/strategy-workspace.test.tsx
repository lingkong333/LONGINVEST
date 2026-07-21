import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import { StrategyBacktestWorkspace, StrategyWorkspace } from "@/features/strategies"
import type { HoldoutBacktestResult, StrategyApi, StrategyDraft } from "@/features/strategies"

vi.mock("@monaco-editor/react", () => ({
  default: ({ value, onChange, options }: {
    value: string
    onChange: (next: string | undefined) => void
    options: { ariaLabel: string }
  }) => <textarea aria-label={options.ariaLabel} value={value} onChange={(event) => onChange(event.target.value)} />,
}))

const draft: StrategyDraft = {
  id: "draft-1",
  strategyId: "strategy-1",
  name: "长期策略",
  description: "月度趋势策略",
  sourceCode: "def calculate_targets(history, params, context):\n    return [1, 2, 3, 4]",
  parameterSchema: '{"type":"object","properties":{}}',
  version: 4,
  updatedAt: "2026-07-21T09:00:00Z",
  allowedActions: ["validate", "test", "archive"],
}

const successResult = { status: "SUCCEEDED", sourceVersion: 4, summary: "检查通过" } as const

function createApi(overrides: Partial<StrategyApi> = {}): StrategyApi {
  return {
    getDraft: vi.fn().mockResolvedValue(draft),
    saveDraft: vi.fn().mockImplementation(async (_id, input) => ({ ...draft, ...input, version: input.expectedVersion + 1 })),
    listRevisions: vi.fn().mockResolvedValue([]),
    restoreRevision: vi.fn().mockResolvedValue({ ...draft, version: 5 }),
    validateDraft: vi.fn().mockResolvedValue(successResult),
    testDraft: vi.fn().mockResolvedValue(successResult),
    publishDraft: vi.fn().mockResolvedValue(successResult),
    archiveStrategy: vi.fn().mockResolvedValue(successResult),
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

describe("策略工作台", () => {
  afterEach(() => vi.useRealTimers())

  it("把真实中文无障碍名称传给 Monaco", async () => {
    renderWorkspace(<StrategyWorkspace strategyId="strategy-1" api={createApi()} />)
    expect(await screen.findByRole("textbox", { name: "Python 策略源码" })).toBeInTheDocument()
  })

  it("参数必须是合法的 JSON Schema", async () => {
    const user = userEvent.setup()
    const api = createApi()
    renderWorkspace(<StrategyWorkspace strategyId="strategy-1" api={api} />)
    const schema = await screen.findByRole("textbox", { name: "参数 JSON Schema" })
    await user.clear(schema)
    await user.type(schema, "不是 JSON")
    await user.click(screen.getByRole("button", { name: "保存" }))
    expect(await screen.findByText("请输入合法的 JSON")).toBeInTheDocument()
    expect(api.saveDraft).not.toHaveBeenCalled()
  })

  it("可以手动保存，并明确显示保存失败", async () => {
    const saveDraft = vi.fn().mockRejectedValue(new Error("offline"))
    const user = userEvent.setup()
    renderWorkspace(<StrategyWorkspace strategyId="strategy-1" api={createApi({ saveDraft })} />)
    await screen.findByRole("textbox", { name: "Python 策略源码" })
    await user.click(screen.getByRole("button", { name: "保存" }))
    expect(await screen.findByText("保存失败，请检查网络后重试。")).toBeInTheDocument()
  })

  it("手动保存成功后给出明确反馈", async () => {
    const user = userEvent.setup()
    renderWorkspace(<StrategyWorkspace strategyId="strategy-1" api={createApi()} />)
    await screen.findByRole("textbox", { name: "Python 策略源码" })
    await user.click(screen.getByRole("button", { name: "保存" }))
    expect(await screen.findByText("草稿已保存")).toBeInTheDocument()
  })

  it("最后一次编辑后等待三十秒才自动保存", async () => {
    const api = createApi()
    renderWorkspace(<StrategyWorkspace strategyId="strategy-1" api={api} />)
    const editor = await screen.findByRole("textbox", { name: "Python 策略源码" })
    vi.useFakeTimers()
    fireEvent.change(editor, { target: { value: "第一次" } })
    await act(async () => { await vi.advanceTimersByTimeAsync(29_000) })
    fireEvent.change(editor, { target: { value: "最后一次" } })
    await act(async () => { await vi.advanceTimersByTimeAsync(29_999) })
    expect(api.saveDraft).not.toHaveBeenCalled()
    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    expect(api.saveDraft).toHaveBeenCalledWith("strategy-1", expect.objectContaining({ sourceCode: "最后一次" }))
  })

  it("冲突时保存三方内容并禁止关闭，人工合并确认后才按服务器版本保存", async () => {
    const server = { ...draft, version: 8, sourceCode: "服务器内容" }
    const saveDraft = vi.fn()
      .mockRejectedValueOnce({ status: 409, current: server })
      .mockResolvedValueOnce({ ...server, version: 9, sourceCode: "人工合并内容" })
    const user = userEvent.setup()
    renderWorkspace(<StrategyWorkspace strategyId="strategy-1" api={createApi({ saveDraft })} />)
    const editor = await screen.findByRole("textbox", { name: "Python 策略源码" })
    await user.clear(editor)
    await user.type(editor, "本地内容")
    await user.click(screen.getByRole("button", { name: "保存" }))
    const dialog = await screen.findByRole("dialog", { name: "保存冲突" })
    expect(dialog).toHaveTextContent("基础版本")
    expect(dialog).toHaveTextContent("本地版本")
    expect(dialog).toHaveTextContent("服务器版本")
    expect(screen.queryByRole("button", { name: "关闭" })).not.toBeInTheDocument()
    await user.keyboard("{Escape}")
    expect(screen.getByRole("dialog", { name: "保存冲突" })).toBeInTheDocument()
    await user.clear(screen.getByRole("textbox", { name: "人工合并结果" }))
    await user.type(screen.getByRole("textbox", { name: "人工合并结果" }), "人工合并内容")
    expect(screen.getByRole("button", { name: "提交人工合并" })).toBeDisabled()
    await user.click(screen.getByRole("checkbox", { name: "我已核对三方内容" }))
    await user.click(screen.getByRole("button", { name: "提交人工合并" }))
    await waitFor(() => expect(saveDraft).toHaveBeenLastCalledWith("strategy-1", expect.objectContaining({ expectedVersion: 8, sourceCode: "人工合并内容" })))
  })

  it("冲突时可以复制本地内容，也可以放弃并采用服务器版本", async () => {
    const server = { ...draft, version: 8, sourceCode: "服务器内容" }
    const user = userEvent.setup()
    const writeText = vi.spyOn(navigator.clipboard, "writeText")
    renderWorkspace(<StrategyWorkspace strategyId="strategy-1" api={createApi({ saveDraft: vi.fn().mockRejectedValue({ status: 409, current: server }) })} />)
    const editor = await screen.findByRole("textbox", { name: "Python 策略源码" })
    await user.clear(editor)
    await user.type(editor, "本地内容")
    await user.click(screen.getByRole("button", { name: "保存" }))
    await user.click(await screen.findByRole("button", { name: "复制本地内容" }))
    expect(writeText).toHaveBeenCalledWith(expect.stringContaining("本地内容"))
    await user.click(screen.getByRole("button", { name: "放弃本地修改并采用服务器版本" }))
    expect(screen.queryByRole("dialog", { name: "保存冲突" })).not.toBeInTheDocument()
    expect(screen.getByRole("textbox", { name: "Python 策略源码" })).toHaveValue("服务器内容")
  })

  it("人工合并仍含冲突标记时禁止提交", async () => {
    const server = { ...draft, version: 8, sourceCode: "服务器内容" }
    const user = userEvent.setup()
    renderWorkspace(<StrategyWorkspace strategyId="strategy-1" api={createApi({ saveDraft: vi.fn().mockRejectedValue({ status: 409, current: server }) })} />)
    const editor = await screen.findByRole("textbox", { name: "Python 策略源码" })
    await user.clear(editor)
    await user.type(editor, "本地内容")
    await user.click(screen.getByRole("button", { name: "保存" }))
    const merged = await screen.findByRole("textbox", { name: "人工合并结果" })
    await user.clear(merged)
    await user.type(merged, "<<<<<<< 本地\n=======\n>>>>>>> 服务器")
    await user.click(screen.getByRole("checkbox", { name: "我已核对三方内容" }))
    expect(screen.getByText("仍有未解决的冲突标记，不能提交。")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "提交人工合并" })).toBeDisabled()
  })

  it("验证前先保存脏草稿，保存失败就中止", async () => {
    const saveDraft = vi.fn().mockRejectedValue(new Error("offline"))
    const validateDraft = vi.fn()
    const user = userEvent.setup()
    renderWorkspace(<StrategyWorkspace strategyId="strategy-1" api={createApi({ saveDraft, validateDraft })} />)
    const editor = await screen.findByRole("textbox", { name: "Python 策略源码" })
    await user.type(editor, "# 修改")
    await user.click(screen.getByRole("button", { name: "验证" }))
    await user.type(screen.getByRole("textbox", { name: "操作原因" }), "准备发布")
    await user.click(screen.getByRole("button", { name: "确认执行" }))
    expect(await screen.findByText("草稿保存失败，后续操作已中止。请检查网络后重试。")).toBeInTheDocument()
    expect(validateDraft).not.toHaveBeenCalled()
  })

  it("源码变化会使旧验证失效，且服务器未允许时不能发布", async () => {
    renderWorkspace(<StrategyWorkspace strategyId="strategy-1" api={createApi()} />)
    const editor = await screen.findByRole("textbox", { name: "Python 策略源码" })
    expect(screen.getByRole("button", { name: "发布" })).toBeDisabled()
    fireEvent.change(editor, { target: { value: "新源码" } })
    expect(screen.getByText("源码已变化，需要重新验证和测试")).toBeInTheDocument()
  })

  it.each([
    ["验证", "validateDraft", "验证已完成"],
    ["测试", "testDraft", "测试已完成"],
    ["归档", "archiveStrategy", "归档已提交"],
  ] as const)("可以确认执行%s并展示运行结果", async (buttonLabel, method, message) => {
    const api = createApi()
    const user = userEvent.setup()
    renderWorkspace(<StrategyWorkspace strategyId="strategy-1" api={api} />)
    await screen.findByRole("textbox", { name: "Python 策略源码" })
    await user.click(screen.getByRole("button", { name: buttonLabel }))
    await user.type(screen.getByRole("textbox", { name: "操作原因" }), "例行检查")
    await user.click(screen.getByRole("button", { name: "确认执行" }))
    expect(await screen.findByText(message)).toBeInTheDocument()
    expect(api[method]).toHaveBeenCalledWith("strategy-1", "例行检查")
  })

  it("只有服务器允许且验证和测试都新鲜时才可发布", async () => {
    const ready = { ...draft, allowedActions: ["validate", "test", "publish", "archive"] as StrategyDraft["allowedActions"], validationResult: successResult, testResult: successResult }
    const api = createApi({ getDraft: vi.fn().mockResolvedValue(ready) })
    const user = userEvent.setup()
    renderWorkspace(<StrategyWorkspace strategyId="strategy-1" api={api} />)
    await screen.findByRole("textbox", { name: "Python 策略源码" })
    expect(screen.getByRole("button", { name: "发布" })).toBeEnabled()
    await user.click(screen.getByRole("button", { name: "发布" }))
    await user.type(screen.getByRole("textbox", { name: "操作原因" }), "发布稳定版本")
    await user.click(screen.getByRole("button", { name: "确认执行" }))
    expect(await screen.findByText("发布已提交")).toBeInTheDocument()
    expect(api.publishDraft).toHaveBeenCalledWith("strategy-1", "发布稳定版本")
  })

  it("可以查看当前草稿与发布版本的差异", async () => {
    const user = userEvent.setup()
    const api = createApi({ listVersions: vi.fn().mockResolvedValue([{ id: "v1", versionNo: 1, status: "PUBLISHED", sourceCodeHash: "a".repeat(64), sourceCode: "旧版源码", publishedAt: "2026-01-01" }]) })
    renderWorkspace(<StrategyWorkspace strategyId="strategy-1" api={api} />)
    await user.click(await screen.findByRole("button", { name: "查看差异" }))
    expect(screen.getByText("当前草稿")).toBeInTheDocument()
    expect(screen.getByText("旧版源码")).toBeInTheDocument()
  })

  it("列表失败不会显示为空，并能确认回滚且防止重复提交", async () => {
    let finishRestore!: (value: StrategyDraft) => void
    const restoreRevision = vi.fn().mockReturnValue(new Promise((resolve) => { finishRestore = resolve }))
    const user = userEvent.setup()
    const api = createApi({
      listRevisions: vi.fn().mockResolvedValue([{ id: "r1", revisionNo: 1, sourceCode: "旧源码", createdAt: "2026-01-01" }]),
      listVersions: vi.fn().mockRejectedValue(new Error("offline")),
      restoreRevision,
    })
    renderWorkspace(<StrategyWorkspace strategyId="strategy-1" api={api} />)
    expect(await screen.findByText("版本列表加载失败，请重试。")).toBeInTheDocument()
    await user.click(screen.getByRole("button", { name: "应用回滚" }))
    await user.type(screen.getByRole("textbox", { name: "操作原因" }), "撤销错误改动")
    await user.click(screen.getByRole("button", { name: "确认执行" }))
    expect(screen.getByRole("button", { name: "处理中" })).toBeDisabled()
    expect(restoreRevision).toHaveBeenCalledTimes(1)
    expect(restoreRevision.mock.calls[0][3]).toBeTruthy()
    finishRestore({ ...draft, version: 5 })
    expect(await screen.findByText("回滚成功")).toBeInTheDocument()
  })

  it("草稿历史列表失败会显示错误，回滚失败也会保留确认窗口", async () => {
    const user = userEvent.setup()
    const api = createApi({ listRevisions: vi.fn().mockRejectedValue(new Error("offline")) })
    renderWorkspace(<StrategyWorkspace strategyId="strategy-1" api={api} />)
    expect(await screen.findByText("草稿历史加载失败，请重试。")).toBeInTheDocument()

    const failing = createApi({
      listRevisions: vi.fn().mockResolvedValue([{ id: "r1", revisionNo: 1, sourceCode: "旧源码", createdAt: "2026-01-01" }]),
      restoreRevision: vi.fn().mockRejectedValue(new Error("failed")),
    })
    renderWorkspace(<StrategyWorkspace strategyId="strategy-2" api={failing} />)
    await user.click(await screen.findByRole("button", { name: "应用回滚" }))
    await user.type(screen.getByRole("textbox", { name: "操作原因" }), "回滚错误版本")
    await user.click(screen.getByRole("button", { name: "确认执行" }))
    expect(await screen.findByText("回滚失败，请重试。")).toBeInTheDocument()
    expect(screen.getByRole("dialog", { name: "确认应用回滚" })).toBeInTheDocument()
  })
})

const result = (status: HoldoutBacktestResult["status"]): HoldoutBacktestResult => ({
  id: "bt-1", status, frozenTargets: [], adjustments: [], trades: [], metrics: [], failureMessage: "执行失败",
})

describe("样本外回测", () => {
  it.each([
    ["QUEUED", "回测正在排队"], ["RUNNING", "回测正在运行"], ["PAUSED", "回测已暂停"],
    ["PARTIAL_SUCCESS", "回测部分成功"], ["FAILED", "回测失败"], ["CANCELED", "回测已取消"],
    ["TIMED_OUT", "回测已超时"], ["OFFLINE", "回测服务离线"],
  ] as const)("显示 %s 状态", async (status, label) => {
    const api = createApi({ getHoldoutBacktest: vi.fn().mockResolvedValue(result(status)) })
    const user = userEvent.setup()
    renderWorkspace(<StrategyBacktestWorkspace strategyId="strategy-1" api={{ ...api, createHoldoutBacktest: vi.fn().mockResolvedValue(result(status)) }} />)
    for (const [name, value] of [["股票代码", "600000.SH"], ["训练开始日期", "2020-01-01"], ["训练结束日期", "2020-12-31"], ["测试开始日期", "2021-01-01"], ["测试结束日期", "2021-12-31"]]) {
      fireEvent.change(screen.getByLabelText(name), { target: { value } })
    }
    await user.click(screen.getByRole("button", { name: "开始样本外回测" }))
    expect(await screen.findByText(label)).toBeInTheDocument()
  })

  it("成功但无交易和指标时明确说明", async () => {
    const api = createApi({ createHoldoutBacktest: vi.fn().mockResolvedValue(result("SUCCEEDED")), getHoldoutBacktest: vi.fn().mockResolvedValue(result("SUCCEEDED")) })
    renderWorkspace(<StrategyBacktestWorkspace strategyId="strategy-1" api={api} />)
    for (const [name, value] of [["股票代码", "600000.SH"], ["训练开始日期", "2020-01-01"], ["训练结束日期", "2020-12-31"], ["测试开始日期", "2021-01-01"], ["测试结束日期", "2021-12-31"]]) fireEvent.change(screen.getByLabelText(name), { target: { value } })
    fireEvent.click(screen.getByRole("button", { name: "开始样本外回测" }))
    expect(await screen.findByText("测试期间没有产生交易")).toBeInTheDocument()
    expect(screen.getByText("暂无可展示的回测指标")).toBeInTheDocument()
  })

  it("展示冻结目标、价格调整、交易和指标，并把方向翻译成中文", async () => {
    const complete: HoldoutBacktestResult = {
      id: "bt-2", status: "SUCCEEDED",
      frozenTargets: [{ label: "强低位", price: "8.00" }],
      adjustments: [{ eventDate: "2021-06-01", factor: "0.9", source: "除权" }],
      trades: [{ date: "2021-06-02", direction: "BUY", price: "8.10", quantity: "100" }],
      metrics: [{ label: "收益率", value: "12%" }],
    }
    const api = createApi({ createHoldoutBacktest: vi.fn().mockResolvedValue(complete), getHoldoutBacktest: vi.fn().mockResolvedValue(complete) })
    renderWorkspace(<StrategyBacktestWorkspace strategyId="strategy-1" api={api} />)
    for (const [name, value] of [["股票代码", "600000.SH"], ["训练开始日期", "2020-01-01"], ["训练结束日期", "2020-12-31"], ["测试开始日期", "2021-01-01"], ["测试结束日期", "2021-12-31"]]) fireEvent.change(screen.getByLabelText(name), { target: { value } })
    fireEvent.click(screen.getByRole("button", { name: "开始样本外回测" }))
    expect(await screen.findByText("8.00")).toBeInTheDocument()
    expect(screen.getByText("0.9")).toBeInTheDocument()
    expect(screen.getByText("买入")).toBeInTheDocument()
    expect(screen.getByText("12%")).toBeInTheDocument()
  })
})
