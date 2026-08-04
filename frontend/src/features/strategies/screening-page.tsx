import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Pause, Play, Plus, RefreshCw, RotateCcw, Square, Trash2 } from "lucide-react"
import { useMemo, useState } from "react"
import { useNavigate } from "react-router-dom"
import { toast } from "sonner"

import { Badge } from "@/shared/ui/badge"
import { Button } from "@/shared/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/shared/ui/card"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/shared/ui/dialog"
import { Field, FieldGroup, FieldLabel } from "@/shared/ui/field"
import { Input } from "@/shared/ui/input"
import { PageState } from "@/shared/ui/page-state"
import { Progress } from "@/shared/ui/progress"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/shared/ui/select"
import { DataTable } from "@/shared/ui/table"

import { createStrategyApi } from "./gateway"
import type { ScreeningBatchDto, ScreeningPeriodDto, StrategyApi } from "./types"

const statusText: Record<string, string> = {
  PENDING: "等待中", RUNNING: "运行中", PAUSING: "暂停中", PAUSED: "已暂停",
  SUCCEEDED: "已完成", PARTIAL: "部分完成", FAILED: "失败", CANCELING: "取消中", CANCELED: "已取消",
  MATCHED: "符合", NOT_MATCHED: "不符合",
}

function emptyPeriod(sequenceNo: number): ScreeningPeriodDto {
  return { sequenceNo, trainingStartDate: "", trainingEndDate: "", testStartDate: "", testEndDate: "" }
}

function validPeriods(periods: ScreeningPeriodDto[]) {
  return periods.every((period, index) => {
    if (!period.trainingStartDate || !period.trainingEndDate || !period.testStartDate || !period.testEndDate) return false
    if (!(period.trainingStartDate <= period.trainingEndDate && period.trainingEndDate < period.testStartDate && period.testStartDate <= period.testEndDate)) return false
    if (index === 0) return true
    const previous = periods[index - 1]
    return period.trainingStartDate >= previous.trainingStartDate
      && period.trainingEndDate >= previous.trainingEndDate
      && period.testStartDate >= previous.testStartDate
      && period.testEndDate >= previous.testEndDate
  })
}

function progress(batch: ScreeningBatchDto) {
  const completed = batch.matchedItems + batch.notMatchedItems + batch.failedItems + batch.canceledItems
  return batch.totalItems ? Math.round(completed * 100 / batch.totalItems) : 0
}

export function StrategyScreeningPage({ api = createStrategyApi() }: { api?: StrategyApi }) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [versionId, setVersionId] = useState("")
  const [concurrency, setConcurrency] = useState("4")
  const [periods, setPeriods] = useState<ScreeningPeriodDto[]>([emptyPeriod(1)])
  const [resultPage, setResultPage] = useState(1)
  const [resultStatus, setResultStatus] = useState("ALL")

  const batches = useQuery({
    queryKey: ["screenings", "batches"],
    queryFn: () => api.listScreenings(1),
    refetchInterval: (query) => query.state.data?.items.some((item) => ["PENDING", "RUNNING", "PAUSING", "CANCELING"].includes(item.status)) ? 3000 : false,
  })
  const selected = batches.data?.items.find((item) => item.id === selectedId) ?? batches.data?.items[0] ?? null
  const versions = useQuery({
    queryKey: ["screenings", "published-versions"],
    queryFn: async () => {
      const strategies = await api.listStrategies()
      const groups = await Promise.all(strategies.items.map(async (strategy) => ({ strategy, versions: await api.listVersions(strategy.id) })))
      return groups.flatMap(({ strategy, versions: values }) => values.filter((item) => item.status === "PUBLISHED").map((item) => ({ id: item.id, label: `${strategy.name} · 版本 ${item.versionNo}` })))
    },
  })
  const results = useQuery({
    queryKey: ["screenings", selected?.id, "results", resultPage, resultStatus],
    queryFn: () => api.listScreeningResults(selected?.id ?? "", resultPage, resultStatus === "ALL" ? undefined : resultStatus),
    enabled: Boolean(selected),
  })
  const refresh = async () => {
    await Promise.all([batches.refetch(), results.refetch()])
  }
  const create = useMutation({
    mutationFn: () => api.createScreening({ strategyVersionId: versionId, periods, concurrency: Number(concurrency) }),
    onSuccess: async (batch) => {
      setSelectedId(batch.id); setCreateOpen(false); toast.success("筛选任务已创建")
      await queryClient.invalidateQueries({ queryKey: ["screenings"] })
    },
  })
  const control = useMutation({
    mutationFn: (action: string) => api.controlScreening(selected?.id ?? "", action),
    onSuccess: async () => { toast.success("操作已受理"); await refresh() },
  })
  const backtest = useMutation({
    mutationFn: () => api.createCandidateBacktest(selected?.id ?? "", "100000", 4),
    onSuccess: (taskId) => { toast.success("回测任务已创建"); navigate(`/backtests/${taskId}`) },
  })
  const versionOptions = useMemo(() => versions.data ?? [], [versions.data])

  if (batches.isPending) return <PageState state="loading" title="正在加载策略筛选" description="正在读取历史筛选批次。" />
  if (batches.isError) return <PageState state="error" title="策略筛选无法加载" description="请检查服务状态后重试。" action={{ label: "重试", onClick: () => void batches.refetch() }} />

  return <main className="mx-auto flex w-full max-w-[96rem] flex-col gap-4 p-4 lg:p-6">
    <header className="flex flex-wrap items-center justify-between gap-3">
      <div><h1 className="text-xl font-semibold">全市场策略筛选</h1><p className="text-sm text-muted-foreground">使用已发布策略和冻结的前复权日线，在多个递增时段中计算候选股票及四档价格。</p></div>
      <div className="flex gap-2"><Button variant="outline" size="sm" onClick={() => void refresh()}><RefreshCw data-icon="inline-start" />刷新</Button><Button size="sm" onClick={() => setCreateOpen(true)}><Plus data-icon="inline-start" />新建筛选</Button></div>
    </header>
    <div className="grid min-w-0 gap-4 xl:grid-cols-[22rem_minmax(0,1fr)]">
      <Card><CardHeader><CardTitle>筛选批次</CardTitle><CardDescription>选择批次查看进度和结果。</CardDescription></CardHeader><CardContent className="flex flex-col gap-2">
        {batches.data?.items.length ? batches.data.items.map((batch) => <Button key={batch.id} variant={batch.id === selected?.id ? "secondary" : "ghost"} className="h-auto justify-start" onClick={() => { setSelectedId(batch.id); setResultPage(1) }}><span className="flex min-w-0 flex-1 flex-col items-start gap-1"><span>{new Date(batch.createdAt).toLocaleString("zh-CN")}</span><span className="text-xs text-muted-foreground">{batch.periods.length} 个时段 · {batch.matchedItems} 只符合 · {statusText[batch.status] ?? batch.status}</span></span></Button>) : <PageState state="empty" title="暂无筛选批次" description="创建后会在这里显示。" />}
      </CardContent></Card>
      {selected ? <div className="flex min-w-0 flex-col gap-4">
        <Card><CardHeader><div className="flex flex-wrap items-start justify-between gap-3"><div><CardTitle>批次进度</CardTitle><CardDescription>{selected.periods.length} 个时段，共 {selected.totalItems} 个股票时段组合。</CardDescription></div><Badge variant="secondary">{statusText[selected.status] ?? selected.status}</Badge></div></CardHeader><CardContent className="flex flex-col gap-3"><Progress value={progress(selected)} /><div className="grid grid-cols-2 gap-2 text-sm md:grid-cols-5"><span>符合 {selected.matchedItems}</span><span>不符合 {selected.notMatchedItems}</span><span>失败 {selected.failedItems}</span><span>待处理 {selected.pendingItems}</span><span>进度 {progress(selected)}%</span></div><div className="flex flex-wrap gap-2">
          {selected.allowedActions.includes("PAUSE") ? <Button variant="outline" size="sm" onClick={() => control.mutate("PAUSE")}><Pause data-icon="inline-start" />暂停</Button> : null}
          {selected.allowedActions.includes("RESUME") ? <Button variant="outline" size="sm" onClick={() => control.mutate("RESUME")}><Play data-icon="inline-start" />继续</Button> : null}
          {selected.allowedActions.includes("CANCEL") ? <Button variant="destructive" size="sm" onClick={() => control.mutate("CANCEL")}><Square data-icon="inline-start" />停止</Button> : null}
          {selected.allowedActions.includes("RETRY_FAILED") ? <Button variant="outline" size="sm" onClick={() => control.mutate("RETRY_FAILED")}><RotateCcw data-icon="inline-start" />重试失败项</Button> : null}
          {selected.status === "SUCCEEDED" && selected.matchedItems > 0 ? <Button size="sm" disabled={backtest.isPending} onClick={() => backtest.mutate()}><Play data-icon="inline-start" />回测候选股票</Button> : null}
        </div></CardContent></Card>
        <Card><CardHeader><div className="flex flex-wrap items-center justify-between gap-3"><div><CardTitle>筛选结果</CardTitle><CardDescription>按股票代码和时段稳定排序。</CardDescription></div><Select value={resultStatus} onValueChange={(value) => { setResultStatus(value); setResultPage(1) }}><SelectTrigger className="w-36"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{["ALL", "MATCHED", "NOT_MATCHED", "FAILED"].map((value) => <SelectItem key={value} value={value}>{value === "ALL" ? "全部结果" : statusText[value]}</SelectItem>)}</SelectGroup></SelectContent></Select></div></CardHeader><CardContent className="flex flex-col gap-3">
          {results.isPending ? <PageState state="loading" title="正在加载结果" description="请稍候。" /> : results.data?.items.length ? <DataTable caption="策略筛选结果" columns={[{ key: "symbol", header: "股票" }, { key: "period", header: "时段" }, { key: "status", header: "结果" }, { key: "prices", header: "四档价格" }, { key: "reason", header: "说明" }]} rows={results.data.items.map((item) => ({ id: item.id, symbol: `${item.symbol} ${item.name}`, period: `第 ${item.periodSequence} 时段`, status: statusText[item.status] ?? item.status, prices: item.values ? `${item.values.lowStrong} / ${item.values.lowWatch} / ${item.values.highWatch} / ${item.values.highStrong}` : "-", reason: item.reason ?? item.failureCode ?? "-" }))} /> : <PageState state="empty" title="暂无结果" description="任务运行后会逐步显示结果。" />}
          {results.data && results.data.total > results.data.pageSize ? <div className="flex items-center justify-end gap-2"><Button variant="outline" size="sm" disabled={resultPage === 1} onClick={() => setResultPage((value) => value - 1)}>上一页</Button><span className="text-sm">第 {resultPage} 页</span><Button variant="outline" size="sm" disabled={resultPage * results.data.pageSize >= results.data.total} onClick={() => setResultPage((value) => value + 1)}>下一页</Button></div> : null}
        </CardContent></Card>
      </div> : null}
    </div>
    <Dialog open={createOpen} onOpenChange={setCreateOpen}><DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-3xl"><DialogTitle>新建全市场筛选</DialogTitle><DialogDescription>每个时段都先用训练期计算四档价格，再判断该股票是否进入候选。日期边界只能向后移动。</DialogDescription><FieldGroup><Field><FieldLabel>已发布策略版本</FieldLabel><Select value={versionId} onValueChange={setVersionId}><SelectTrigger><SelectValue placeholder="选择策略版本" /></SelectTrigger><SelectContent><SelectGroup>{versionOptions.map((item) => <SelectItem key={item.id} value={item.id}>{item.label}</SelectItem>)}</SelectGroup></SelectContent></Select></Field><Field><FieldLabel htmlFor="screening-concurrency">并发数</FieldLabel><Input id="screening-concurrency" type="number" min="1" max="64" value={concurrency} onChange={(event) => setConcurrency(event.target.value)} /></Field></FieldGroup>
      <div className="flex flex-col gap-3">{periods.map((period, index) => <Card key={period.sequenceNo}><CardHeader><div className="flex items-center justify-between"><CardTitle>第 {index + 1} 时段</CardTitle>{periods.length > 1 ? <Button size="icon-sm" variant="ghost" title="删除时段" onClick={() => setPeriods((values) => values.filter((_, valueIndex) => valueIndex !== index).map((value, valueIndex) => ({ ...value, sequenceNo: valueIndex + 1 })))}><Trash2 /></Button> : null}</div></CardHeader><CardContent><FieldGroup className="grid md:grid-cols-2"><Field><FieldLabel>训练开始</FieldLabel><Input type="date" value={period.trainingStartDate} onChange={(event) => setPeriods((values) => values.map((value, valueIndex) => valueIndex === index ? { ...value, trainingStartDate: event.target.value } : value))} /></Field><Field><FieldLabel>训练结束</FieldLabel><Input type="date" value={period.trainingEndDate} onChange={(event) => setPeriods((values) => values.map((value, valueIndex) => valueIndex === index ? { ...value, trainingEndDate: event.target.value } : value))} /></Field><Field><FieldLabel>测试开始</FieldLabel><Input type="date" value={period.testStartDate} onChange={(event) => setPeriods((values) => values.map((value, valueIndex) => valueIndex === index ? { ...value, testStartDate: event.target.value } : value))} /></Field><Field><FieldLabel>测试结束</FieldLabel><Input type="date" value={period.testEndDate} onChange={(event) => setPeriods((values) => values.map((value, valueIndex) => valueIndex === index ? { ...value, testEndDate: event.target.value } : value))} /></Field></FieldGroup></CardContent></Card>)}</div>
      <Button variant="outline" disabled={periods.length >= 20} onClick={() => setPeriods((values) => [...values, emptyPeriod(values.length + 1)])}><Plus data-icon="inline-start" />增加时段</Button>
      <DialogFooter><Button variant="outline" onClick={() => setCreateOpen(false)}>取消</Button><Button disabled={!versionId || !validPeriods(periods) || Number(concurrency) < 1 || Number(concurrency) > 64 || create.isPending} onClick={() => create.mutate()}>确认创建</Button></DialogFooter>
    </DialogContent></Dialog>
  </main>
}
