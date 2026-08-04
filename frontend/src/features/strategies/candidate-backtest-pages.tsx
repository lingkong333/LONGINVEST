import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ArrowLeft, Pause, Play, RefreshCw, RotateCcw, Search, Square } from "lucide-react"
import { useState } from "react"
import { Link, useNavigate, useParams } from "react-router-dom"
import { Area, AreaChart, CartesianGrid, ReferenceDot, ReferenceLine, XAxis, YAxis } from "recharts"
import { toast } from "sonner"

import { Badge } from "@/shared/ui/badge"
import { Button } from "@/shared/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/shared/ui/card"
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from "@/shared/ui/chart"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/shared/ui/dialog"
import { Field, FieldGroup, FieldLabel } from "@/shared/ui/field"
import { Input } from "@/shared/ui/input"
import { PageState } from "@/shared/ui/page-state"
import { Progress } from "@/shared/ui/progress"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/shared/ui/select"
import { DataTable } from "@/shared/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/shared/ui/tabs"

import { createStrategyApi } from "./gateway"
import type { BacktestAction, PriceVersionDto, StrategyApi, TargetValuesDto } from "./types"

const taskStatus: Record<string, string> = { PENDING: "等待中", RUNNING: "运行中", PAUSING: "暂停中", PAUSED: "已暂停", SUCCEEDED: "已完成", PARTIAL: "部分完成", FAILED: "失败", CANCELING: "取消中", CANCELED: "已取消" }
const itemStatus: Record<string, string> = { PENDING: "等待中", FETCHING_DATA: "读取数据", VALIDATING_DATA: "校验数据", SIMULATING: "模拟交易", SAVING: "保存结果", SUCCEEDED: "成功", FAILED: "失败", CANCELED: "已取消" }

function taskProgress(summary?: { totalItems: number; completedItems: number }) {
  return summary?.totalItems ? Math.round(summary.completedItems * 100 / summary.totalItems) : 0
}

export function CandidateBacktestsPage({ api = createStrategyApi() }: { api?: StrategyApi }) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [page, setPage] = useState(1)
  const [search, setSearch] = useState("")
  const [status, setStatus] = useState("ALL")
  const [period, setPeriod] = useState("ALL")
  const tasks = useQuery({ queryKey: ["candidate-backtests"], queryFn: () => api.listCandidateBacktests(1), refetchInterval: 4000 })
  const selected = tasks.data?.items.find((item) => item.taskId === selectedId) ?? tasks.data?.items[0] ?? null
  const summary = useQuery({ queryKey: ["candidate-backtests", selected?.taskId, "summary"], queryFn: () => api.getHoldoutBacktestSummary(selected?.taskId ?? ""), enabled: Boolean(selected), refetchInterval: 3000 })
  const items = useQuery({
    queryKey: ["candidate-backtests", selected?.taskId, "items", page, search, status, period],
    queryFn: () => api.listCandidateItems(selected?.taskId ?? "", page, { status: status === "ALL" ? undefined : status, search: search || undefined, periodSequence: period === "ALL" ? undefined : Number(period) }),
    enabled: Boolean(selected),
  })
  const control = useMutation({
    mutationFn: (action: BacktestAction) => api.controlHoldoutBacktest(selected?.taskId ?? "", action, "用户在回测任务页执行控制操作"),
    onSuccess: async () => { toast.success("操作已受理"); await queryClient.invalidateQueries({ queryKey: ["candidate-backtests"] }) },
  })
  if (tasks.isPending) return <PageState state="loading" title="正在加载回测任务" description="正在读取候选批次回测。" />
  if (tasks.isError) return <PageState state="error" title="回测任务无法加载" description="请检查服务后重试。" action={{ label: "重试", onClick: () => void tasks.refetch() }} />
  return <main className="mx-auto flex w-full max-w-[96rem] flex-col gap-4 p-4 lg:p-6">
    <header className="flex flex-wrap items-center justify-between gap-3"><div><h1 className="text-xl font-semibold">回测任务</h1><p className="text-sm text-muted-foreground">只基于已完成的冻结筛选批次创建，每只股票的每个时段独立使用相同初始资金。</p></div><Button variant="outline" size="sm" onClick={() => void tasks.refetch()}><RefreshCw data-icon="inline-start" />刷新</Button></header>
    <div className="grid min-w-0 gap-4 xl:grid-cols-[22rem_minmax(0,1fr)]">
      <Card><CardHeader><CardTitle>任务列表</CardTitle><CardDescription>最近的候选批次回测。</CardDescription></CardHeader><CardContent className="flex flex-col gap-2">{tasks.data?.items.length ? tasks.data.items.map((task) => <Button key={task.taskId} variant={task.taskId === selected?.taskId ? "secondary" : "ghost"} className="h-auto justify-start" onClick={() => { setSelectedId(task.taskId); setPage(1) }}><span className="flex min-w-0 flex-1 flex-col items-start gap-1"><span>{new Date(task.createdAt).toLocaleString("zh-CN")}</span><span className="text-xs text-muted-foreground">{task.item.symbol} 等 · {taskStatus[task.status] ?? task.status}</span></span></Button>) : <PageState state="empty" title="暂无回测任务" description="请先在策略筛选页完成筛选并启动回测。" action={{ label: "前往策略筛选", onClick: () => navigate("/screenings") }} />}</CardContent></Card>
      {selected ? <div className="flex min-w-0 flex-col gap-4"><Card><CardHeader><div className="flex flex-wrap items-start justify-between gap-3"><div><CardTitle>任务进度</CardTitle><CardDescription>批次 {selected.screeningBatchId}</CardDescription></div><Badge variant="secondary">{taskStatus[summary.data?.status ?? selected.status] ?? selected.status}</Badge></div></CardHeader><CardContent className="flex flex-col gap-3"><Progress value={taskProgress(summary.data)} /><div className="grid grid-cols-2 gap-2 text-sm md:grid-cols-5"><span>总数 {summary.data?.totalItems ?? 0}</span><span>成功 {summary.data?.succeededItems ?? 0}</span><span>失败 {summary.data?.failedItems ?? 0}</span><span>待处理 {summary.data?.pendingItems ?? 0}</span><span>进度 {taskProgress(summary.data)}%</span></div><div className="flex flex-wrap gap-2">{summary.data?.allowedActions.includes("PAUSE") ? <Button variant="outline" size="sm" onClick={() => control.mutate("PAUSE")}><Pause data-icon="inline-start" />暂停</Button> : null}{summary.data?.allowedActions.includes("RESUME") ? <Button variant="outline" size="sm" onClick={() => control.mutate("RESUME")}><Play data-icon="inline-start" />继续</Button> : null}{summary.data?.allowedActions.includes("CANCEL") ? <Button variant="destructive" size="sm" onClick={() => control.mutate("CANCEL")}><Square data-icon="inline-start" />停止</Button> : null}{summary.data?.allowedActions.includes("RETRY_FAILED") ? <Button variant="outline" size="sm" onClick={() => control.mutate("RETRY_FAILED")}><RotateCcw data-icon="inline-start" />重试失败项</Button> : null}</div></CardContent></Card>
        <Card><CardHeader><CardTitle>股票与时段结果</CardTitle><CardDescription>支持数据库分页、状态、股票和时段筛选。</CardDescription></CardHeader><CardContent className="flex flex-col gap-3"><div className="grid gap-2 md:grid-cols-[minmax(0,1fr)_10rem_10rem]"><div className="relative"><Search className="pointer-events-none absolute left-3 top-2.5 text-muted-foreground" aria-hidden="true" /><Input aria-label="搜索股票" className="pl-9" placeholder="股票代码或名称" value={search} onChange={(event) => { setSearch(event.target.value); setPage(1) }} /></div><Select value={status} onValueChange={(value) => { setStatus(value); setPage(1) }}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="ALL">全部状态</SelectItem>{["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELED"].map((value) => <SelectItem key={value} value={value}>{itemStatus[value] ?? value}</SelectItem>)}</SelectGroup></SelectContent></Select><Select value={period} onValueChange={(value) => { setPeriod(value); setPage(1) }}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="ALL">全部时段</SelectItem>{Array.from({ length: 20 }, (_, index) => <SelectItem key={index + 1} value={String(index + 1)}>第 {index + 1} 时段</SelectItem>)}</SelectGroup></SelectContent></Select></div>
          {items.isPending ? <PageState state="loading" title="正在加载结果" description="请稍候。" /> : items.data?.items.length ? <DataTable caption="回测股票与时段结果" columns={[{ key: "stock", header: "股票" }, { key: "period", header: "时段" }, { key: "status", header: "状态" }, { key: "attempts", header: "尝试次数" }, { key: "action", header: "操作" }]} rows={items.data.items.map((item) => ({ id: item.itemId, stock: `${item.symbol} ${item.name}`, period: item.periodSequence ? `第 ${item.periodSequence} 时段` : "-", status: itemStatus[item.status] ?? item.status, attempts: item.attemptCount, action: <Button size="sm" variant="outline" onClick={() => navigate(`/backtests/${selected.taskId}/items/${item.itemId}`)}>查看详情</Button> }))} /> : <PageState state="empty" title="没有符合条件的结果" description="请调整筛选条件。" />}
          {items.data && items.data.total > items.data.pageSize ? <div className="flex items-center justify-end gap-2"><Button size="sm" variant="outline" disabled={page === 1} onClick={() => setPage((value) => value - 1)}>上一页</Button><span className="text-sm">第 {page} 页</span><Button size="sm" variant="outline" disabled={page * items.data.pageSize >= items.data.total} onClick={() => setPage((value) => value + 1)}>下一页</Button></div> : null}</CardContent></Card></div> : null}
    </div>
  </main>
}

const chartConfig = { closePrice: { label: "收盘价", color: "var(--chart-1)" } } satisfies ChartConfig

function PriceEditor({ version, onSave, pending }: { version: PriceVersionDto; onSave: (effectiveDate: string, values: TargetValuesDto, reason: string) => void; pending: boolean }) {
  const [effectiveDate, setEffectiveDate] = useState(version.effectiveDate)
  const [values, setValues] = useState(version.values)
  const [reason, setReason] = useState("")
  const ordered = Number(values.lowStrong) < Number(values.lowWatch) && Number(values.lowWatch) < Number(values.highWatch) && Number(values.highWatch) < Number(values.highStrong)
  return <FieldGroup><Field><FieldLabel>生效日期</FieldLabel><Input type="date" value={effectiveDate} onChange={(event) => setEffectiveDate(event.target.value)} /></Field><div className="grid gap-3 md:grid-cols-2">{([['lowStrong', '强支撑价'], ['lowWatch', '弱支撑价'], ['highWatch', '弱压力价'], ['highStrong', '强压力价']] as const).map(([key, label]) => <Field key={key}><FieldLabel>{label}</FieldLabel><Input type="number" step="0.01" value={values[key]} onChange={(event) => setValues((current) => ({ ...current, [key]: event.target.value }))} /></Field>)}</div><Field><FieldLabel>修改原因</FieldLabel><Input value={reason} maxLength={500} onChange={(event) => setReason(event.target.value)} /></Field><Button disabled={!ordered || !reason.trim() || pending} onClick={() => onSave(effectiveDate, values, reason)}>保存新版本并重算</Button></FieldGroup>
}

export function CandidateBacktestDetailPage({ api = createStrategyApi() }: { api?: StrategyApi }) {
  const { taskId = "", itemId = "" } = useParams()
  const queryClient = useQueryClient()
  const [editOpen, setEditOpen] = useState(false)
  const detail = useQuery({ queryKey: ["candidate-backtest", taskId, itemId], queryFn: () => api.getCandidateItem(taskId, itemId) })
  const versions = useQuery({ queryKey: ["candidate-backtest", taskId, itemId, "prices"], queryFn: () => api.listPriceVersions(taskId, itemId) })
  const latest = versions.data?.[0]
  const change = useMutation({ mutationFn: (input: { effectiveDate: string; values: TargetValuesDto; reason: string }) => api.changePriceVersion(taskId, itemId, { ...input, expectedVersion: latest?.versionNo ?? 1 }), onSuccess: async () => { setEditOpen(false); toast.success("价格版本已保存，正在重算"); await queryClient.invalidateQueries({ queryKey: ["candidate-backtest", taskId, itemId] }) } })
  const rollback = useMutation({ mutationFn: (version: PriceVersionDto) => api.rollbackPriceVersion(taskId, itemId, { sourceVersionId: version.id, effectiveDate: version.effectiveDate, expectedVersion: latest?.versionNo ?? 1, reason: `回滚到版本 ${version.versionNo}` }), onSuccess: async () => { toast.success("已创建回滚版本，正在重算"); await queryClient.invalidateQueries({ queryKey: ["candidate-backtest", taskId, itemId] }) } })
  if (detail.isPending || versions.isPending) return <PageState state="loading" title="正在加载回测详情" description="正在读取图表、交易和价格版本。" />
  if (detail.isError || !detail.data) return <PageState state="error" title="回测详情无法加载" description="请返回任务列表后重试。" />
  const result = detail.data
  const targets = latest?.values ?? result.forecast?.values
  const tradesByDate = new Map(result.trades.map((trade) => [trade.executeDate, trade]))
  const chartData = result.dailyResults.map((row) => ({ date: row.tradeDate, closePrice: Number(row.closePrice), trade: tradesByDate.get(row.tradeDate) }))
  const metrics = result.metrics
  return <main className="mx-auto flex w-full max-w-[96rem] flex-col gap-4 p-4 lg:p-6"><header className="flex flex-wrap items-center justify-between gap-3"><div className="flex items-center gap-3"><Button asChild size="icon-sm" variant="ghost" title="返回任务"><Link to={`/backtests/${taskId}`}><ArrowLeft /></Link></Button><div><h1 className="text-xl font-semibold">回测结果详情</h1><p className="text-sm text-muted-foreground">日线收盘价、四档价格、交易点与收益明细。</p></div></div>{latest ? <Button onClick={() => setEditOpen(true)}>调整四档价格</Button> : null}</header>
    <Card><CardHeader><CardTitle>价格与交易图</CardTitle><CardDescription>价格版本 {latest?.versionNo ?? 1}；修改价格后从最早生效日期重算。</CardDescription></CardHeader><CardContent>{chartData.length ? <ChartContainer config={chartConfig} className="h-[28rem] w-full aspect-auto"><AreaChart data={chartData} margin={{ left: 8, right: 16 }}><defs><linearGradient id="backtest-close" x1="0" y1="0" x2="0" y2="1"><stop offset="5%" stopColor="var(--color-closePrice)" stopOpacity={0.35}/><stop offset="95%" stopColor="var(--color-closePrice)" stopOpacity={0.03}/></linearGradient></defs><CartesianGrid vertical={false}/><XAxis dataKey="date" minTickGap={32}/><YAxis domain={["auto", "auto"]}/><ChartTooltip content={<ChartTooltipContent />} /><Area dataKey="closePrice" type="monotone" stroke="var(--color-closePrice)" fill="url(#backtest-close)" />{targets ? <>{Object.entries(targets).map(([key, value]) => <ReferenceLine key={key} y={Number(value)} stroke="var(--muted-foreground)" strokeDasharray="4 4" label={value} />)}</> : null}{chartData.filter((point) => point.trade).map((point) => <ReferenceDot key={point.date} x={point.date} y={point.closePrice} r={5} fill={point.trade?.direction === "BUY" ? "var(--chart-2)" : "var(--chart-5)"} stroke="var(--background)" />)}</AreaChart></ChartContainer> : <PageState state="empty" title="暂无日线结果" description="任务完成后会显示图表。" />}</CardContent></Card>
    <div className="grid gap-4 lg:grid-cols-4">{[["总收益", metrics?.totalReturn], ["净盈亏", metrics?.netProfitAmount], ["最大回撤", metrics?.maxDrawdown], ["胜率", metrics?.winRate ?? "无完整交易"]].map(([label, value]) => <Card key={label}><CardHeader><CardDescription>{label}</CardDescription><CardTitle>{value ?? "-"}</CardTitle></CardHeader></Card>)}</div>
    <Card><CardContent className="pt-6"><Tabs defaultValue="trades"><TabsList><TabsTrigger value="trades">交易明细</TabsTrigger><TabsTrigger value="metrics">完整指标</TabsTrigger><TabsTrigger value="versions">价格版本</TabsTrigger></TabsList><TabsContent value="trades">{result.trades.length ? <DataTable caption="模拟交易明细" columns={[{ key: "date", header: "日期" }, { key: "direction", header: "方向" }, { key: "price", header: "成交价" }, { key: "quantity", header: "数量" }, { key: "return", header: "已实现盈亏" }]} rows={result.trades.map((trade) => ({ id: trade.id, date: trade.executeDate, direction: trade.direction === "BUY" ? "买入" : "卖出", price: trade.price, quantity: trade.quantity, return: trade.realizedReturnAmount ?? "-" }))} /> : <PageState state="empty" title="没有成交记录" description="该测试期没有触发完整交易。" />}</TabsContent><TabsContent value="metrics">{metrics ? <DataTable caption="回测指标" columns={[{ key: "name", header: "指标" }, { key: "value", header: "数值" }]} rows={Object.entries(metrics).map(([name, value]) => ({ id: name, name, value: value === null ? "-" : String(value) }))} /> : null}</TabsContent><TabsContent value="versions"><DataTable caption="四档价格版本" columns={[{ key: "version", header: "版本" }, { key: "effective", header: "生效日期" }, { key: "prices", header: "四档价格" }, { key: "source", header: "来源" }, { key: "reason", header: "原因" }, { key: "action", header: "操作" }]} rows={(versions.data ?? []).map((version) => ({ id: version.id, version: version.versionNo, effective: version.effectiveDate, prices: `${version.values.lowStrong} / ${version.values.lowWatch} / ${version.values.highWatch} / ${version.values.highStrong}`, source: version.source, reason: version.reason, action: version.id === latest?.id ? <Badge variant="secondary">当前</Badge> : <Button size="sm" variant="outline" disabled={rollback.isPending} onClick={() => rollback.mutate(version)}>回滚</Button> }))} /></TabsContent></Tabs></CardContent></Card>
    <Dialog open={editOpen} onOpenChange={setEditOpen}><DialogContent><DialogTitle>调整四档价格</DialogTitle><DialogDescription>新版本从指定日期开始生效，历史版本会保留，相关结果自动重算。</DialogDescription>{latest ? <PriceEditor version={latest} pending={change.isPending} onSave={(effectiveDate, values, reason) => change.mutate({ effectiveDate, values, reason })} /> : null}<DialogFooter><Button variant="outline" onClick={() => setEditOpen(false)}>关闭</Button></DialogFooter></DialogContent></Dialog>
  </main>
}
