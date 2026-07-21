import { useMutation, useQuery } from "@tanstack/react-query"
import { useRef, useState } from "react"
import { z } from "zod"

import { useZodForm } from "@/shared/forms/use-zod-form"
import { Button } from "@/shared/ui/button"
import { FormField } from "@/shared/ui/form-field"
import { Input } from "@/shared/ui/input"
import { PageState } from "@/shared/ui/page-state"
import { DataTable } from "@/shared/ui/table"

import type { HoldoutBacktestInput, HoldoutBacktestResult, HoldoutBacktestStatus, StrategyApi } from "./types"

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

const stateCopy: Record<Exclude<HoldoutBacktestStatus, "SUCCEEDED">, { title: string; description: string; state: "loading" | "error" | "empty" }> = {
  QUEUED: { title: "回测正在排队", description: "任务已受理，正在等待空闲执行资源。", state: "loading" },
  RUNNING: { title: "回测正在运行", description: "正在使用冻结目标模拟测试期交易。", state: "loading" },
  PAUSED: { title: "回测已暂停", description: "任务已暂停，不会继续推进。", state: "empty" },
  PARTIAL_SUCCESS: { title: "回测部分成功", description: "部分结果可用，请同时查看失败说明。", state: "empty" },
  FAILED: { title: "回测失败", description: "任务未能完成。", state: "error" },
  CANCELED: { title: "回测已取消", description: "任务已取消，不会继续运行。", state: "empty" },
  TIMED_OUT: { title: "回测已超时", description: "任务超过允许时长后已停止。", state: "error" },
  OFFLINE: { title: "回测服务离线", description: "执行服务当前不可用，请稍后重试。", state: "error" },
}

function ResultDetails({ result }: { result: HoldoutBacktestResult }) {
  return <div className="space-y-6">
    <section><h2 className="text-lg font-semibold">冻结目标价格</h2>{result.frozenTargets.length ? <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">{result.frozenTargets.map((target) => <div key={target.label} className="border border-border bg-card p-3"><p className="m-0 text-xs text-muted-foreground">{target.label}</p><strong>{target.price}</strong></div>)}</div> : <p className="text-sm text-muted-foreground">暂无冻结目标价格</p>}</section>
    <section><h2 className="text-lg font-semibold">目标调整记录</h2>{result.adjustments.length ? <DataTable caption="目标调整记录" columns={[{ key: "eventDate", header: "发生日期" }, { key: "factor", header: "调整因子" }, { key: "source", header: "调整来源" }]} rows={result.adjustments.map((item, index) => ({ ...item, id: `${item.eventDate}-${index}` }))} /> : <p className="text-sm text-muted-foreground">测试期间没有发生目标调整</p>}</section>
    <section><h2 className="text-lg font-semibold">模拟交易</h2>{result.trades.length ? <DataTable caption="样本外模拟交易" columns={[{ key: "date", header: "日期" }, { key: "direction", header: "方向" }, { key: "price", header: "价格" }, { key: "quantity", header: "数量" }]} rows={result.trades.map((trade, index) => ({ ...trade, direction: trade.direction === "BUY" ? "买入" : "卖出", id: `${trade.date}-${index}` }))} /> : <p className="text-sm text-muted-foreground">测试期间没有产生交易</p>}</section>
    <section><h2 className="text-lg font-semibold">回测指标</h2>{result.metrics.length ? <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">{result.metrics.map((metric) => <div key={metric.label} className="border border-border bg-card p-3"><dt className="text-xs text-muted-foreground">{metric.label}</dt><dd className="m-0 font-semibold">{metric.value}</dd></div>)}</dl> : <p className="text-sm text-muted-foreground">暂无可展示的回测指标</p>}</section>
  </div>
}

function BacktestResult({ result }: { result: HoldoutBacktestResult }) {
  if (result.status === "SUCCEEDED") return <ResultDetails result={result} />
  const copy = stateCopy[result.status]
  return <div className="space-y-5"><PageState state={copy.state} title={copy.title} description={result.failureMessage ?? copy.description} />{result.status === "PARTIAL_SUCCESS" ? <ResultDetails result={result} /> : null}</div>
}

export function StrategyBacktestWorkspace({ strategyId, api }: { strategyId: string; api: StrategyApi }) {
  const [backtestId, setBacktestId] = useState<string | null>(null)
  const [initialResult, setInitialResult] = useState<HoldoutBacktestResult | null>(null)
  const pollCount = useRef(0)
  const form = useZodForm(holdoutSchema, { defaultValues: { securitySymbol: "", trainingStartDate: "", trainingEndDate: "", testStartDate: "", testEndDate: "" } })
  const createMutation = useMutation({ mutationFn: (input: HoldoutBacktestInput) => api.createHoldoutBacktest(input), onSuccess: (result) => { pollCount.current = 0; setInitialResult(result); setBacktestId(result.id) } })
  const resultQuery = useQuery({
    queryKey: ["strategies", strategyId, "holdout", backtestId],
    queryFn: async () => { pollCount.current += 1; return api.getHoldoutBacktest(backtestId ?? "") },
    enabled: backtestId !== null,
    refetchInterval: (query) => {
      const status = query.state.data?.status ?? initialResult?.status
      return pollCount.current < 40 && (status === "RUNNING" || status === "QUEUED") ? 3_000 : false
    },
  })
  const submit = form.handleSubmit((values) => createMutation.mutate({ strategyId, ...values }))
  const displayedResult = resultQuery.data ?? initialResult

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
    {resultQuery.isError ? <PageState state="error" title="回测结果无法加载" description="请重试获取结果。" action={{ label: "重试", onClick: () => void resultQuery.refetch() }} /> : displayedResult ? <BacktestResult result={displayedResult} /> : null}
  </section>
}
