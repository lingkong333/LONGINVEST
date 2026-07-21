import { useMutation, useQuery } from "@tanstack/react-query"
import { useRef, useState } from "react"
import { z } from "zod"

import { useZodForm } from "@/shared/forms/use-zod-form"
import { Button } from "@/shared/ui/button"
import { FormField } from "@/shared/ui/form-field"
import { Input } from "@/shared/ui/input"
import { PageState } from "@/shared/ui/page-state"
import { DataTable } from "@/shared/ui/table"

import type { BacktestItemStatus, BacktestTaskStatus, HoldoutBacktestInput, HoldoutBacktestResult, StrategyApi, TargetValuesDto } from "./types"

const maximumAutomaticPolls = 40
const activeTaskStatuses = new Set<BacktestTaskStatus>(["PENDING", "RUNNING", "PAUSING", "CANCELING"])

const holdoutSchema = z.object({
  securitySymbol: z.string().regex(/^\d{6}\.(SH|SZ|BJ)$/, "请输入六位 A 股代码，例如 600000.SH"),
  trainingStartDate: z.string().min(1, "请选择训练开始日期"),
  trainingEndDate: z.string().min(1, "请选择训练结束日期"),
  testStartDate: z.string().min(1, "请选择测试开始日期"),
  testEndDate: z.string().min(1, "请选择测试结束日期"),
}).superRefine((value, context) => {
  if (value.trainingEndDate && value.testStartDate && value.trainingEndDate >= value.testStartDate) context.addIssue({ code: "custom", path: ["testStartDate"], message: "测试期必须在训练期结束后开始" })
  if (value.trainingStartDate && value.trainingEndDate && value.trainingStartDate > value.trainingEndDate) context.addIssue({ code: "custom", path: ["trainingEndDate"], message: "训练结束日期不能早于开始日期" })
  if (value.testStartDate && value.testEndDate && value.testStartDate > value.testEndDate) context.addIssue({ code: "custom", path: ["testEndDate"], message: "测试结束日期不能早于开始日期" })
})

const taskCopy: Record<BacktestTaskStatus, { title: string; description: string; state: "loading" | "error" | "empty" }> = {
  PENDING: { title: "回测正在排队", description: "任务已受理，正在等待执行资源。", state: "loading" },
  RUNNING: { title: "回测正在运行", description: "正在使用冻结目标模拟测试期交易。", state: "loading" },
  PAUSING: { title: "回测正在暂停", description: "正在安全停止当前处理步骤。", state: "loading" },
  PAUSED: { title: "回测已暂停", description: "任务已暂停，不会继续推进。", state: "empty" },
  SUCCEEDED: { title: "回测成功", description: "回测已经完成。", state: "empty" },
  PARTIAL: { title: "回测部分成功", description: "部分结果可用，请查看失败说明。", state: "empty" },
  FAILED: { title: "回测失败", description: "任务未能完成。", state: "error" },
  CANCELING: { title: "回测正在取消", description: "正在安全停止回测任务。", state: "loading" },
  CANCELED: { title: "回测已取消", description: "任务已取消，不会继续运行。", state: "empty" },
}

const itemStatusLabels: Record<BacktestItemStatus, string> = {
  PENDING: "单股任务等待中",
  FETCHING_DATA: "正在获取行情数据",
  VALIDATING_DATA: "正在校验行情数据",
  FORECASTING: "正在计算目标价格",
  FROZEN: "目标价格已冻结",
  SIMULATING: "正在模拟交易",
  SAVING: "正在保存回测结果",
  SUCCEEDED: "单股回测成功",
  FAILED: "单股回测失败",
  SKIPPED: "单股回测已跳过",
  CANCELED: "单股回测已取消",
}

function isTaskStatus(value: string): value is BacktestTaskStatus {
  return value in taskCopy
}

function isItemStatus(value: string): value is BacktestItemStatus {
  return value in itemStatusLabels
}

function isActiveStatus(value: string): boolean {
  return isTaskStatus(value) && activeTaskStatuses.has(value)
}

function targetLine(values: TargetValuesDto): string {
  return `${values.lowStrong} / ${values.lowWatch} / ${values.highWatch} / ${values.highStrong}`
}

function ItemStatus({ result }: { result: HoldoutBacktestResult }) {
  if (!result.item) return null
  const label = isItemStatus(result.item.status) ? itemStatusLabels[result.item.status] : `未知单股状态：${result.item.status}`
  return <p role="status" className="text-sm">{label}{result.item.failureMessage ? `：${result.item.failureMessage}` : ""}</p>
}

function ResultDetails({ result }: { result: HoldoutBacktestResult }) {
  const forecast = result.forecast
  const metricEntries = result.metrics ? [
    ["期末权益", result.metrics.endingEquity], ["总收益率", result.metrics.totalReturn], ["已实现收益", result.metrics.realizedReturn],
    ["年化收益率", result.metrics.annualizedReturn], ["最大回撤", result.metrics.maxDrawdown], ["波动率", result.metrics.volatility],
    ["夏普比率", result.metrics.sharpeRatio ?? "不可用"], ["完整交易轮次", result.metrics.completedRoundTrips], ["盈利交易", result.metrics.winningTrades],
    ["亏损交易", result.metrics.losingTrades], ["持平交易", result.metrics.breakevenTrades], ["胜率", result.metrics.winRate ?? "不可用"],
    ["平均单笔收益", result.metrics.averageTradeReturn ?? "不可用"], ["最大单笔盈利", result.metrics.maximumTradeGain ?? "不可用"],
    ["最大单笔亏损", result.metrics.maximumTradeLoss ?? "不可用"], ["平均持有交易日", result.metrics.averageHoldingTradeDays ?? "不可用"],
    ["最长持有交易日", result.metrics.longestHoldingTradeDays], ["资金暴露比例", result.metrics.capitalExposureRatio],
    ["期末仍有持仓", result.metrics.openPositionAtEnd ? "是" : "否"], ["未成交订单数", result.metrics.unfilledOrderCount],
  ] : []
  return <div className="space-y-6">
    <section><h2 className="text-lg font-semibold">冻结目标快照</h2>{forecast ? <div className="space-y-2"><p>{targetLine(forecast.values)}</p><p className="text-sm">单股任务：{forecast.itemId}；训练期：{forecast.trainingStartDate} 至 {forecast.trainingEndDate}，共 {forecast.trainingRowCount} 条；获取时间：{forecast.trainingFetchedAt}</p><p className="break-all text-xs text-muted-foreground">训练数据摘要：{forecast.trainingDataHash}；源码摘要：{forecast.sourceCodeHash}；参数摘要：{forecast.parameterHash}</p><p className="break-all text-sm">价格口径：{forecast.priceBasis}；环境：{forecast.environmentVersion}；执行镜像：{forecast.runnerImageDigest}；冻结时间：{forecast.frozenAt}</p><pre className="overflow-auto bg-muted p-2 text-xs">{JSON.stringify(forecast.diagnostics, null, 2)}</pre></div> : <p className="text-sm text-muted-foreground">暂无冻结目标价格</p>}</section>
    <section><h2 className="text-lg font-semibold">目标调整记录</h2>{result.adjustments.length ? <DataTable caption="目标调整记录" columns={[{ key: "eventDate", header: "发生日期" }, { key: "before", header: "调整前" }, { key: "after", header: "调整后" }, { key: "factor", header: "调整因子" }, { key: "source", header: "来源" }, { key: "times", header: "发布时间 / 生效时间" }]} rows={result.adjustments.map((item, index) => ({ id: `${item.eventDate}-${index}`, eventDate: `${item.eventDate} (${item.itemId})`, before: targetLine(item.beforeValues), after: targetLine(item.afterValues), factor: item.adjustmentFactor, source: `${item.source} (${item.dataHash})`, times: `${item.publishedAt} / ${item.effectiveAt}` }))} /> : <p className="text-sm text-muted-foreground">测试期间没有发生目标调整</p>}</section>
    <section><h2 className="text-lg font-semibold">模拟订单</h2>{result.orders.length ? <DataTable caption="样本外模拟订单" columns={[{ key: "signalDate", header: "信号日" }, { key: "executeDate", header: "执行日" }, { key: "status", header: "状态" }, { key: "direction", header: "方向" }, { key: "price", header: "成交价" }, { key: "quantity", header: "数量" }, { key: "balances", header: "下单前资金 / 持仓" }, { key: "target", header: "目标 / 区间" }]} rows={result.orders.map((order) => ({ id: order.id, signalDate: order.signalDate, executeDate: order.executeDate ?? "未执行", status: order.status === "FILLED" ? "已成交" : order.status === "PENDING" ? "等待成交" : "期末未成交", direction: order.direction === "BUY" ? "买入" : "卖出", price: order.executionPrice ?? "未成交", quantity: order.quantity, balances: `${order.cashBefore} / ${order.positionBefore}`, target: `${targetLine(order.targetValues)} / ${order.targetZone}` }))} /> : <p className="text-sm text-muted-foreground">测试期间没有产生订单</p>}</section>
    <section><h2 className="text-lg font-semibold">模拟交易</h2>{result.trades.length ? <DataTable caption="样本外模拟交易" columns={[{ key: "executeDate", header: "日期" }, { key: "direction", header: "方向" }, { key: "price", header: "价格" }, { key: "quantity", header: "数量" }, { key: "balances", header: "成交后资金 / 持仓" }, { key: "roundTrip", header: "轮次 / 持有日" }, { key: "returns", header: "已实现收益" }, { key: "target", header: "目标 / 区间" }]} rows={result.trades.map((trade) => ({ id: trade.id, executeDate: trade.executeDate, direction: trade.direction === "BUY" ? "买入" : "卖出", price: trade.price, quantity: trade.quantity, balances: `${trade.cashAfter} / ${trade.positionAfter}`, roundTrip: `${trade.roundTripNo} / ${trade.holdingTradeDays ?? "-"}`, returns: `${trade.realizedReturnAmount ?? "-"} / ${trade.realizedReturnRate ?? "-"}`, target: `${targetLine(trade.targetValues)} / ${trade.targetZone} / ${trade.orderId}` }))} /> : <p className="text-sm text-muted-foreground">测试期间没有产生交易</p>}</section>
    <section><h2 className="text-lg font-semibold">回测指标</h2>{metricEntries.length ? <div><p className="text-xs text-muted-foreground">单股任务：{result.metrics?.itemId}</p><dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">{metricEntries.map(([label, value]) => <div key={String(label)} className="border border-border bg-card p-3"><dt className="text-xs text-muted-foreground">{label}</dt><dd className="m-0 font-semibold">{String(value)}</dd></div>)}</dl></div> : <p className="text-sm text-muted-foreground">暂无可展示的回测指标</p>}</section>
    <section><h2 className="text-lg font-semibold">每日权益</h2>{result.dailyResults.length ? <DataTable caption="每日权益结果" columns={[{ key: "tradeDate", header: "交易日" }, { key: "cash", header: "现金" }, { key: "position", header: "持仓数量 / 市值" }, { key: "closePrice", header: "收盘价" }, { key: "equity", header: "权益 / 回撤" }, { key: "target", header: "目标 / 区间 / 状态" }]} rows={result.dailyResults.map((day) => ({ id: `${day.itemId}-${day.tradeDate}`, tradeDate: day.tradeDate, cash: day.cash, position: `${day.positionQuantity} / ${day.positionMarketValue}`, closePrice: day.closePrice, equity: `${day.equity} / ${day.drawdown}`, target: `${targetLine(day.targetValues)} / ${day.zone} / ${day.positionStatus === "HOLDING" ? "持仓" : "空仓"}` }))} /> : <p className="text-sm text-muted-foreground">暂无每日权益结果</p>}</section>
  </div>
}

function BacktestResult({ result }: { result: HoldoutBacktestResult }) {
  if (!isTaskStatus(result.status)) return <div className="space-y-3"><PageState state="error" title={`未知任务状态：${result.status}`} description="服务器返回了当前页面尚不识别的状态，自动轮询已停止。" /><ItemStatus result={result} /></div>
  if (result.status === "SUCCEEDED") return <div className="space-y-4"><ItemStatus result={result} /><ResultDetails result={result} /></div>
  const copy = taskCopy[result.status]
  return <div className="space-y-5"><PageState state={copy.state} title={copy.title} description={result.failureMessage ?? copy.description} /><ItemStatus result={result} />{result.status === "PARTIAL" ? <ResultDetails result={result} /> : null}</div>
}

export function StrategyBacktestWorkspace({ strategyId, api }: { strategyId: string; api: StrategyApi }) {
  const [backtestId, setBacktestId] = useState<string | null>(null)
  const [initialResult, setInitialResult] = useState<HoldoutBacktestResult | null>(null)
  const [pollingStopped, setPollingStopped] = useState(false)
  const pollCount = useRef(0)
  const form = useZodForm(holdoutSchema, { defaultValues: { securitySymbol: "", trainingStartDate: "", trainingEndDate: "", testStartDate: "", testEndDate: "" } })
  const createMutation = useMutation({ mutationFn: (input: HoldoutBacktestInput) => api.createHoldoutBacktest(input), onSuccess: (result) => { pollCount.current = 0; setPollingStopped(false); setInitialResult(result); setBacktestId(result.id) } })
  const resultQuery = useQuery({
    queryKey: ["strategies", strategyId, "holdout", backtestId],
    queryFn: async () => {
      pollCount.current += 1
      const result = await api.getHoldoutBacktest(backtestId ?? "")
      if (pollCount.current >= maximumAutomaticPolls && isActiveStatus(result.status)) setPollingStopped(true)
      return result
    },
    enabled: backtestId !== null,
    refetchInterval: (query) => {
      const status = query.state.data?.status ?? initialResult?.status ?? ""
      return pollCount.current < maximumAutomaticPolls && isActiveStatus(status) ? 3_000 : false
    },
  })
  const submit = form.handleSubmit((values) => createMutation.mutate({ strategyId, ...values }))
  const displayedResult = resultQuery.data ?? initialResult
  const continuePolling = () => { pollCount.current = 0; setPollingStopped(false); void resultQuery.refetch() }

  return <section className="mx-auto grid w-full max-w-5xl gap-6 p-4 lg:p-6">
    <header><p className="text-sm font-medium text-muted-foreground">单只股票固定目标样本外验证</p><h1 className="m-0 text-2xl font-semibold">样本外回测</h1><p className="mt-2 max-w-2xl text-sm text-muted-foreground">策略只能读取训练期数据，测试期数据不会进入策略沙箱。训练完成后四档目标在测试期冻结，除权除息只调整价格口径。</p></header>
    <form className="grid gap-4 rounded border border-border bg-card p-4 md:grid-cols-2" onSubmit={submit}>
      <FormField control={form.control} name="securitySymbol" label="股票代码">{({ field }) => <Input placeholder="600000.SH" {...field} />}</FormField><div className="hidden md:block" />
      <FormField control={form.control} name="trainingStartDate" label="训练开始日期">{({ field }) => <Input type="date" {...field} />}</FormField>
      <FormField control={form.control} name="trainingEndDate" label="训练结束日期">{({ field }) => <Input type="date" {...field} />}</FormField>
      <FormField control={form.control} name="testStartDate" label="测试开始日期">{({ field }) => <Input type="date" {...field} />}</FormField>
      <FormField control={form.control} name="testEndDate" label="测试结束日期">{({ field }) => <Input type="date" {...field} />}</FormField>
      <div className="md:col-span-2"><Button type="submit" disabled={createMutation.isPending}>{createMutation.isPending ? "正在启动回测" : "开始样本外回测"}</Button>{createMutation.isError ? <p role="alert" className="mt-2 text-sm text-destructive">回测启动失败，请稍后重试。</p> : null}</div>
    </form>
    {pollingStopped ? <div role="alert" className="border border-amber-500 p-3 text-sm"><p>自动轮询已达到 40 次上限并停止。</p><Button type="button" variant="secondary" onClick={continuePolling}>手动继续查询</Button></div> : null}
    {resultQuery.isError ? <PageState state="error" title="回测结果无法加载" description="请重试获取结果。" action={{ label: "重试", onClick: () => void resultQuery.refetch() }} /> : displayedResult ? <BacktestResult result={displayedResult} /> : null}
  </section>
}
