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

  it("拒绝语法正确但不是合法结构的 JSON Schema", async () => {
    const user = userEvent.setup()
    const api = createApi()
    renderWorkspace(<StrategyWorkspace strategyId="strategy-1" api={api} />)
    const schema = await screen.findByRole("textbox", { name: "参数 JSON Schema" })
    fireEvent.change(schema, { target: { value: '{"type":"unknown","properties":[]}' } })
    await user.click(screen.getByRole("button", { name: "保存" }))
    expect(await screen.findByText("请输入合法的 JSON Schema")).toBeInTheDocument()
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

  it("冲突基础版本固定为本次编辑起点，且每个字段默认保留服务器值", async () => {
    const starting = { ...draft, sourceCode: "编辑起点", name: "起点名称" }
    const server = { ...draft, version: 8, sourceCode: "服务器源码", name: "服务器名称", description: "服务器说明", parameterSchema: '{"type":"string"}' }
    const saveDraft = vi.fn().mockRejectedValueOnce({ status: 409, current: server }).mockResolvedValueOnce({ ...server, version: 9 })
    const user = userEvent.setup()
    renderWorkspace(<StrategyWorkspace strategyId="strategy-1" api={createApi({ getDraft: vi.fn().mockResolvedValue(starting), saveDraft })} />)
    const editor = await screen.findByRole("textbox", { name: "Python 策略源码" })
    starting.sourceCode = "外部对象后来被修改"
    await user.clear(editor)
    await user.type(editor, "本地源码")
    await user.click(screen.getByRole("button", { name: "保存" }))
    expect(await screen.findByText("编辑起点")).toBeInTheDocument()
    for (const field of ["策略名称", "策略说明", "参数 JSON Schema", "Python 策略源码"]) {
      expect(screen.getByRole("radio", { name: `${field}采用服务器版本` })).toBeChecked()
    }
    await user.click(screen.getByRole("checkbox", { name: "我已核对三方内容" }))
    await user.click(screen.getByRole("button", { name: "提交人工合并" }))
    await waitFor(() => expect(saveDraft).toHaveBeenLastCalledWith("strategy-1", expect.objectContaining({
      expectedVersion: 8,
      name: "服务器名称",
      description: "服务器说明",
      parameterSchema: '{"type":"string"}',
      sourceCode: "服务器源码",
    })))
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

  it("归档前保存草稿，并按保存响应的新权限中止归档", async () => {
    const saved = { ...draft, version: 5, allowedActions: ["validate"] as StrategyDraft["allowedActions"] }
    const saveDraft = vi.fn().mockResolvedValue(saved)
    const archiveStrategy = vi.fn()
    const user = userEvent.setup()
    renderWorkspace(<StrategyWorkspace strategyId="strategy-1" api={createApi({ saveDraft, archiveStrategy })} />)
    await user.type(await screen.findByRole("textbox", { name: "Python 策略源码" }), "# 修改")
    await user.click(screen.getByRole("button", { name: "归档" }))
    await user.type(screen.getByRole("textbox", { name: "操作原因" }), "归档旧策略")
    await user.click(screen.getByRole("button", { name: "确认执行" }))
    expect(await screen.findByText("服务器已不允许执行此操作，操作已中止。")).toBeInTheDocument()
    expect(saveDraft).toHaveBeenCalled()
    expect(archiveStrategy).not.toHaveBeenCalled()
  })

  it("接口正常返回但运行状态失败时按失败展示", async () => {
    const validateDraft = vi.fn().mockResolvedValue({ status: "FAILED", sourceVersion: 4, summary: "语法检查失败", details: ["第 2 行错误"] })
    const user = userEvent.setup()
    renderWorkspace(<StrategyWorkspace strategyId="strategy-1" api={createApi({ validateDraft })} />)
    await screen.findByRole("textbox", { name: "Python 策略源码" })
    await user.click(screen.getByRole("button", { name: "验证" }))
    await user.type(screen.getByRole("textbox", { name: "操作原因" }), "检查语法")
    await user.click(screen.getByRole("button", { name: "确认执行" }))
    expect(await screen.findByRole("alert")).toHaveTextContent("语法检查失败")
    expect(screen.getByText("第 2 行错误")).toBeInTheDocument()
  })

  it("归档返回取消状态时不显示成功，并保留结果原因", async () => {
    const archiveStrategy = vi.fn().mockResolvedValue({ status: "CANCELED", sourceVersion: 4, summary: "归档请求已取消", details: ["策略仍被任务使用"] })
    const user = userEvent.setup()
    renderWorkspace(<StrategyWorkspace strategyId="strategy-1" api={createApi({ archiveStrategy })} />)
    await screen.findByRole("textbox", { name: "Python 策略源码" })
    await user.click(screen.getByRole("button", { name: "归档" }))
    await user.type(screen.getByRole("textbox", { name: "操作原因" }), "归档旧策略")
    await user.click(screen.getByRole("button", { name: "确认执行" }))
    expect(await screen.findByRole("alert")).toHaveTextContent("归档请求已取消")
    expect(screen.getByText("策略仍被任务使用")).toBeInTheDocument()
    expect(screen.queryByText("归档已完成")).not.toBeInTheDocument()
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
    ["归档", "archiveStrategy", "归档已完成"],
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
    expect(await screen.findByText("发布已完成")).toBeInTheDocument()
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
  id: "bt-1", status, forecast: null, adjustments: [], orders: [], trades: [], metrics: null, dailyResults: [],
})

describe("样本外回测", () => {
  afterEach(() => vi.useRealTimers())

  it.each([
    ["PENDING", "回测正在排队"], ["RUNNING", "回测正在运行"], ["PAUSING", "回测正在暂停"],
    ["PAUSED", "回测已暂停"], ["PARTIAL", "回测部分成功"], ["FAILED", "回测失败"],
    ["CANCELING", "回测正在取消"], ["CANCELED", "回测已取消"],
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

  it.each([
    ["FETCHING_DATA", "正在获取行情数据"], ["VALIDATING_DATA", "正在校验行情数据"],
    ["FORECASTING", "正在计算目标价格"], ["FROZEN", "目标价格已冻结"],
    ["SIMULATING", "正在模拟交易"], ["SAVING", "正在保存回测结果"],
  ] as const)("展示单股 %s 处理阶段", async (itemStatus, label) => {
    const current = { ...result("PAUSED"), item: { status: itemStatus } }
    const api = createApi({ createHoldoutBacktest: vi.fn().mockResolvedValue(current), getHoldoutBacktest: vi.fn().mockResolvedValue(current) })
    renderWorkspace(<StrategyBacktestWorkspace strategyId="strategy-1" api={api} />)
    for (const [name, value] of [["股票代码", "600000.SH"], ["训练开始日期", "2020-01-01"], ["训练结束日期", "2020-12-31"], ["测试开始日期", "2021-01-01"], ["测试结束日期", "2021-12-31"]]) fireEvent.change(screen.getByLabelText(name), { target: { value } })
    fireEvent.click(screen.getByRole("button", { name: "开始样本外回测" }))
    expect(await screen.findByText(label)).toBeInTheDocument()
  })

  it("使用定时器按状态序列轮询，并在暂停后停止", async () => {
    vi.useFakeTimers()
    const states = [result("PENDING"), result("RUNNING"), result("PAUSING"), result("PAUSED")]
    const getHoldoutBacktest = vi.fn().mockImplementation(async () => states.shift() ?? result("PAUSED"))
    const api = createApi({ createHoldoutBacktest: vi.fn().mockResolvedValue(result("PENDING")), getHoldoutBacktest })
    renderWorkspace(<StrategyBacktestWorkspace strategyId="strategy-1" api={api} />)
    for (const [name, value] of [["股票代码", "600000.SH"], ["训练开始日期", "2020-01-01"], ["训练结束日期", "2020-12-31"], ["测试开始日期", "2021-01-01"], ["测试结束日期", "2021-12-31"]]) fireEvent.change(screen.getByLabelText(name), { target: { value } })
    fireEvent.click(screen.getByRole("button", { name: "开始样本外回测" }))
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    expect(getHoldoutBacktest).toHaveBeenCalledTimes(4)
    expect(screen.getByText("回测已暂停")).toBeInTheDocument()
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(getHoldoutBacktest).toHaveBeenCalledTimes(4)
  })

  it("四十次自动查询后停止，并允许手动继续", async () => {
    vi.useFakeTimers()
    const getHoldoutBacktest = vi.fn().mockImplementation(async () => getHoldoutBacktest.mock.calls.length <= 40 ? result("RUNNING") : result("PAUSED"))
    const api = createApi({ createHoldoutBacktest: vi.fn().mockResolvedValue(result("PENDING")), getHoldoutBacktest })
    renderWorkspace(<StrategyBacktestWorkspace strategyId="strategy-1" api={api} />)
    for (const [name, value] of [["股票代码", "600000.SH"], ["训练开始日期", "2020-01-01"], ["训练结束日期", "2020-12-31"], ["测试开始日期", "2021-01-01"], ["测试结束日期", "2021-12-31"]]) fireEvent.change(screen.getByLabelText(name), { target: { value } })
    fireEvent.click(screen.getByRole("button", { name: "开始样本外回测" }))
    await act(async () => { await vi.advanceTimersByTimeAsync(120_000) })
    expect(getHoldoutBacktest).toHaveBeenCalledTimes(40)
    expect(screen.getByText("自动轮询已达到 40 次上限并停止。")).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "手动继续查询" }))
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(getHoldoutBacktest).toHaveBeenCalledTimes(41)
    expect(screen.getByText("回测已暂停")).toBeInTheDocument()
  })

  it("未知任务和单股状态会安全降级", async () => {
    const unknown = { ...result("NEW_SERVER_STATE"), item: { status: "NEW_ITEM_STATE" } }
    const api = createApi({ createHoldoutBacktest: vi.fn().mockResolvedValue(unknown), getHoldoutBacktest: vi.fn().mockResolvedValue(unknown) })
    renderWorkspace(<StrategyBacktestWorkspace strategyId="strategy-1" api={api} />)
    for (const [name, value] of [["股票代码", "600000.SH"], ["训练开始日期", "2020-01-01"], ["训练结束日期", "2020-12-31"], ["测试开始日期", "2021-01-01"], ["测试结束日期", "2021-12-31"]]) fireEvent.change(screen.getByLabelText(name), { target: { value } })
    fireEvent.click(screen.getByRole("button", { name: "开始样本外回测" }))
    expect(await screen.findByText("未知任务状态：NEW_SERVER_STATE")).toBeInTheDocument()
    expect(screen.getByText("未知单股状态：NEW_ITEM_STATE")).toBeInTheDocument()
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
      item: { status: "SUCCEEDED" },
      forecast: { itemId: "item1", trainingStartDate: "2020-01-01", trainingEndDate: "2020-12-31", trainingRowCount: 240, trainingFetchedAt: "2021-01-01T00:00:00Z", trainingDataHash: "a".repeat(64), sourceCodeHash: "b".repeat(64), parameterHash: "c".repeat(64), values: { lowStrong: "8.00", lowWatch: "9.00", highWatch: "12.00", highStrong: "13.00" }, diagnostics: { rows: 240 }, environmentVersion: "1", runnerImageDigest: `sha256:${"d".repeat(64)}`, priceBasis: "QFQ", frozenAt: "2021-01-01T00:00:01Z" },
      adjustments: [{ itemId: "item1", eventDate: "2021-06-01", beforeValues: { lowStrong: "8.00", lowWatch: "9.00", highWatch: "12.00", highStrong: "13.00" }, afterValues: { lowStrong: "7.20", lowWatch: "8.10", highWatch: "10.80", highStrong: "11.70" }, adjustmentFactor: "0.9", source: "除权", dataHash: "e".repeat(64), publishedAt: "2021-05-01T00:00:00Z", effectiveAt: "2021-06-01T00:00:00Z" }],
      orders: [{ id: "o1", itemId: "item1", signalDate: "2021-06-01", executeDate: "2021-06-02", status: "FILLED", direction: "BUY", executionPrice: "8.10", quantity: "100", cashBefore: "1000", positionBefore: "0", targetValues: { lowStrong: "7.20", lowWatch: "8.10", highWatch: "10.80", highStrong: "11.70" }, targetZone: "LOW" }],
      trades: [{ id: "t1", itemId: "item1", orderId: "o1", executeDate: "2021-06-02", direction: "BUY", price: "8.10", quantity: "100", cashAfter: "190", positionAfter: "100", targetValues: { lowStrong: "7.20", lowWatch: "8.10", highWatch: "10.80", highStrong: "11.70" }, targetZone: "LOW", roundTripNo: 1, holdingTradeDays: null, realizedReturnAmount: null, realizedReturnRate: null }],
      metrics: { itemId: "item1", endingEquity: "1120", totalReturn: "0.12", realizedReturn: "0", annualizedReturn: "0.12", maxDrawdown: "0.03", volatility: "0.1", sharpeRatio: "1.2", completedRoundTrips: 0, winningTrades: 0, losingTrades: 0, breakevenTrades: 0, winRate: null, averageTradeReturn: null, maximumTradeGain: null, maximumTradeLoss: null, averageHoldingTradeDays: null, longestHoldingTradeDays: 0, capitalExposureRatio: "0.8", openPositionAtEnd: true, unfilledOrderCount: 0 },
      dailyResults: [{ itemId: "item1", tradeDate: "2021-06-02", cash: "190", positionQuantity: "100", closePrice: "9.30", positionMarketValue: "930", equity: "1120", drawdown: "0", targetValues: { lowStrong: "7.20", lowWatch: "8.10", highWatch: "10.80", highStrong: "11.70" }, zone: "NORMAL", positionStatus: "HOLDING" }],
    }
    const api = createApi({ createHoldoutBacktest: vi.fn().mockResolvedValue(complete), getHoldoutBacktest: vi.fn().mockResolvedValue(complete) })
    renderWorkspace(<StrategyBacktestWorkspace strategyId="strategy-1" api={api} />)
    for (const [name, value] of [["股票代码", "600000.SH"], ["训练开始日期", "2020-01-01"], ["训练结束日期", "2020-12-31"], ["测试开始日期", "2021-01-01"], ["测试结束日期", "2021-12-31"]]) fireEvent.change(screen.getByLabelText(name), { target: { value } })
    fireEvent.click(screen.getByRole("button", { name: "开始样本外回测" }))
    expect(await screen.findAllByText("8.00 / 9.00 / 12.00 / 13.00")).toHaveLength(2)
    expect(screen.getByText("0.9")).toBeInTheDocument()
    expect(screen.getByText("7.20 / 8.10 / 10.80 / 11.70")).toBeInTheDocument()
    expect(screen.getByText("已成交")).toBeInTheDocument()
    expect(screen.getAllByText("买入")).toHaveLength(2)
    expect(screen.getAllByText("0.12").length).toBeGreaterThanOrEqual(2)
  })
})
