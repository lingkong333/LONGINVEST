import { useState, type ReactNode } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  ActivityIcon,
  AlertTriangleIcon,
  CalendarRangeIcon,
  ChevronRightIcon,
  DatabaseIcon,
  HistoryIcon,
  SearchIcon,
  ShieldCheckIcon,
} from "lucide-react"

import { marketDataGateway } from "@/features/market-data/gateway"
import type {
  MarketDataGateway,
  QualityIssueAction,
  QualityIssueSummary,
} from "@/features/market-data/types"
import { ApiError } from "@/shared/api/client"
import { Button } from "@/shared/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogTitle,
} from "@/shared/ui/dialog"
import { Input } from "@/shared/ui/input"
import { PageState } from "@/shared/ui/page-state"

interface MarketDataPageProps {
  gateway?: MarketDataGateway
}

function dateTime(value: string | null) {
  if (!value) return "—"
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value))
}

function statusTone(status: string) {
  if (["READY", "COMPLETED", "SUCCEEDED", "CURRENT", "FRESH", "RESOLVED"].includes(status)) {
    return "border-emerald-600/30 bg-emerald-600/10 text-emerald-800"
  }
  if (["FAILED", "CONFLICT", "CRITICAL", "STALE"].includes(status)) {
    return "border-destructive/30 bg-destructive/10 text-destructive"
  }
  if (["PARTIAL", "WARNING", "PAUSED", "OPEN"].includes(status)) {
    return "border-amber-600/30 bg-amber-600/10 text-amber-800"
  }
  return "border-border bg-muted/60 text-muted-foreground"
}

const statusLabels: Record<string, string> = {
  ACTIVE: "正常",
  CANCELED: "已取消",
  COMPLETED: "已完成",
  CRITICAL: "严重",
  CURRENT: "当前生效",
  FAILED: "失败",
  FETCHING: "抓取中",
  FINALIZING: "收尾中",
  FRESH: "最新",
  INVALIDATED: "已标无效",
  LISTED: "上市",
  MISSED: "已错过",
  OPEN: "待处理",
  PARTIAL: "部分完成",
  PAUSED: "已暂停",
  PENDING: "等待中",
  READY: "可用",
  RESOLVED: "已解决",
  RUNNING: "运行中",
  STALE: "已过期",
  SUCCEEDED: "成功",
  WARNING: "警告",
}

const issueTypeLabels: Record<string, string> = {
  DAILY_BAR_CONFLICT: "日线来源冲突",
  DAILY_BAR_INVALID: "日线数据异常",
  MISSING_DAILY_BAR: "日线缺失",
  QUOTE_CONFLICT: "实时行情冲突",
  QUOTE_INVALID: "实时行情异常",
  SOURCE_CONFLICT: "来源冲突",
}

function Status({ value }: { value: string }) {
  return (
    <span className={`inline-flex rounded border px-2 py-0.5 text-xs font-medium ${statusTone(value)}`}>
      {statusLabels[value] ?? value}
    </span>
  )
}

function Panel({
  title,
  description,
  icon: Icon,
  children,
}: {
  title: string
  description: string
  icon: typeof DatabaseIcon
  children: ReactNode
}) {
  return (
    <section className="min-w-0 border-t pt-5">
      <header className="mb-4 flex items-start gap-3">
        <div className="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded border bg-muted/50">
          <Icon className="size-4" aria-hidden="true" />
        </div>
        <div className="min-w-0">
          <h2 className="text-base font-semibold">{title}</h2>
          <p className="mt-0.5 text-sm text-muted-foreground">{description}</p>
        </div>
      </header>
      {children}
    </section>
  )
}

function SectionError({
  error,
  retry,
  label,
}: {
  error: Error
  retry: () => void
  label: string
}) {
  return (
    <PageState
      state="error"
      title={`${label}暂时无法读取`}
      description="其他区域仍可继续使用。请稍后重试这一项。"
      action={{ label: `重新加载${label}`, onClick: retry }}
      error={error instanceof ApiError ? {
        code: error.code,
        requestId: error.requestId,
      } : { code: "UNKNOWN_ERROR" }}
    />
  )
}

export function MarketDataPage({ gateway = marketDataGateway }: MarketDataPageProps) {
  const queryClient = useQueryClient()
  const [selectedCycleId, setSelectedCycleId] = useState<string | null>(null)
  const [qfqInput, setQfqInput] = useState("")
  const [qfqSymbol, setQfqSymbol] = useState("")
  const [qualityCommand, setQualityCommand] = useState<{
    issue: QualityIssueSummary
    action: QualityIssueAction
  } | null>(null)
  const [qualityReason, setQualityReason] = useState("")
  const [selectedSource, setSelectedSource] = useState("")

  const securities = useQuery({
    queryKey: ["market-data", "securities"],
    queryFn: () => gateway.loadSecurities(),
  })
  const quoteCycles = useQuery({
    queryKey: ["market-data", "quote-cycles"],
    queryFn: () => gateway.loadQuoteCycles(),
  })
  const quoteItems = useQuery({
    queryKey: ["market-data", "quote-items", selectedCycleId],
    queryFn: () => gateway.loadQuoteItems(selectedCycleId ?? ""),
    enabled: selectedCycleId !== null,
  })
  const dailyBatches = useQuery({
    queryKey: ["market-data", "daily-batches"],
    queryFn: () => gateway.loadDailyBatches(),
  })
  const qfq = useQuery({
    queryKey: ["market-data", "qfq", qfqSymbol],
    queryFn: () => gateway.loadQfq(qfqSymbol),
    enabled: qfqSymbol.length > 0,
    retry: false,
  })
  const qualityIssues = useQuery({
    queryKey: ["market-data", "quality-issues"],
    queryFn: () => gateway.loadQualityIssues(),
  })
  const backfills = useQuery({
    queryKey: ["market-data", "backfills"],
    queryFn: () => gateway.loadBackfills(),
  })
  const qualityMutation = useMutation({
    mutationFn: () => {
      if (!qualityCommand) return Promise.resolve()
      return gateway.runQualityAction({
        issueId: qualityCommand.issue.id,
        action: qualityCommand.action,
        reason: qualityReason.trim(),
        selectedSource: qualityCommand.action === "SELECT_SOURCE"
          ? selectedSource
          : undefined,
      })
    },
    onSuccess: async () => {
      setQualityCommand(null)
      setQualityReason("")
      setSelectedSource("")
      await queryClient.invalidateQueries({
        queryKey: ["market-data", "quality-issues"],
      })
    },
  })

  function openQualityCommand(
    issue: QualityIssueSummary,
    action: QualityIssueAction,
  ) {
    setQualityCommand({ issue, action })
    setQualityReason("")
    setSelectedSource(issue.sourceCandidates[0] ?? "")
    qualityMutation.reset()
  }

  return (
    <main className="mx-auto w-full max-w-[1500px] px-4 py-5 sm:px-6">
      <header className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">行情数据中心</h1>
        </div>
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <ShieldCheckIcon className="size-4 text-emerald-700" aria-hidden="true" />
          价格仅由数据源采集，不支持人工录入
        </div>
      </header>

      <div className="grid gap-x-8 gap-y-7 xl:grid-cols-2">
        <Panel
          title="证券主数据"
          description="当前股票身份、上市状态和主数据版本"
          icon={DatabaseIcon}
        >
          {securities.isPending ? (
            <PageState state="loading" title="正在读取证券主数据" description="正在加载最新主数据版本。" />
          ) : securities.isError ? (
            <SectionError error={securities.error} retry={() => void securities.refetch()} label="证券主数据" />
          ) : securities.data.items.length === 0 ? (
            <PageState state="empty" title="还没有证券主数据" description="系统尚未形成可展示的证券清单。" />
          ) : (
            <div className="overflow-x-auto border">
              <table className="w-full min-w-[620px] text-sm">
                <thead className="bg-muted/50 text-left text-xs text-muted-foreground">
                  <tr>
                    <th className="px-3 py-2 font-medium">代码 / 名称</th>
                    <th className="px-3 py-2 font-medium">市场</th>
                    <th className="px-3 py-2 font-medium">状态</th>
                    <th className="px-3 py-2 text-right font-medium">版本</th>
                    <th className="px-3 py-2 text-right font-medium">更新时间</th>
                  </tr>
                </thead>
                <tbody>
                  {securities.data.items.slice(0, 8).map((item) => (
                    <tr key={item.id} className="border-t">
                      <td className="px-3 py-2.5">
                        <strong className="font-medium">{item.symbol}</strong>
                        <span className="ml-2 text-muted-foreground">{item.name}</span>
                        {item.isSt || item.isSuspended ? (
                          <AlertTriangleIcon className="ml-2 inline size-3.5 text-amber-700" aria-label="存在特别状态" />
                        ) : null}
                      </td>
                      <td className="px-3 py-2.5">{item.market}</td>
                      <td className="px-3 py-2.5"><Status value={item.listingStatus} /></td>
                      <td className="px-3 py-2.5 text-right tabular-nums">v{item.masterVersion}</td>
                      <td className="px-3 py-2.5 text-right text-muted-foreground">{dateTime(item.updatedAt)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="border-t px-3 py-2 text-xs text-muted-foreground">
                共 {securities.data.pagination.total} 只，当前显示前 {Math.min(8, securities.data.items.length)} 只
              </p>
            </div>
          )}
        </Panel>

        <Panel
          title="实时采集周期"
          description="批次屏障状态及逐股有效性诊断"
          icon={ActivityIcon}
        >
          {quoteCycles.isPending ? (
            <PageState state="loading" title="正在读取实时采集周期" description="正在加载最近批次。" />
          ) : quoteCycles.isError ? (
            <SectionError error={quoteCycles.error} retry={() => void quoteCycles.refetch()} label="实时采集周期" />
          ) : quoteCycles.data.items.length === 0 ? (
            <PageState state="empty" title="还没有实时采集周期" description="当前没有可展示的采集批次。" />
          ) : (
            <div className="space-y-2">
              {quoteCycles.data.items.slice(0, 5).map((cycle) => (
                <button
                  type="button"
                  key={cycle.id}
                  className="flex w-full items-center gap-3 border px-3 py-2.5 text-left hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  aria-label={`查看批次 ${cycle.id} 明细`}
                  onClick={() => setSelectedCycleId(cycle.id)}
                >
                  <Status value={cycle.status} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium">{dateTime(cycle.scheduledAt)}</span>
                    <span className="block text-xs text-muted-foreground">
                      有效 {cycle.validCount}/{cycle.expectedCount} · 缺失 {cycle.missingCount} · 冲突 {cycle.conflictCount} · 失败 {cycle.failedCount}
                    </span>
                  </span>
                  <ChevronRightIcon className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                </button>
              ))}
              {selectedCycleId ? (
                <div className="mt-3 border border-dashed p-3">
                  <div className="mb-2 flex items-center justify-between gap-3">
                    <h3 className="truncate text-sm font-medium">批次逐股诊断</h3>
                    <Button size="sm" variant="ghost" onClick={() => setSelectedCycleId(null)}>关闭</Button>
                  </div>
                  {quoteItems.isPending ? (
                    <p className="text-sm text-muted-foreground">正在读取批次明细…</p>
                  ) : quoteItems.isError ? (
                    <p role="alert" className="text-sm text-destructive">批次明细读取失败，主列表不受影响。</p>
                  ) : quoteItems.data?.length === 0 ? (
                    <p className="text-sm text-muted-foreground">该批次没有逐股记录。</p>
                  ) : (
                    <div className="max-h-48 overflow-auto">
                      {quoteItems.data?.map((item) => (
                        <div key={item.id} className="grid grid-cols-[1fr_auto_auto] gap-3 border-t py-2 text-sm first:border-t-0">
                          <span>{item.symbol} <span className="text-muted-foreground">{item.provider ?? "无来源"}</span></span>
                          <span className="tabular-nums">{item.price ?? "—"}</span>
                          <Status value={item.errorCode ?? item.status} />
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ) : null}
            </div>
          )}
        </Panel>

        <Panel
          title="日线批次"
          description="全市场不复权日线提交与缺失概况"
          icon={CalendarRangeIcon}
        >
          {dailyBatches.isPending ? (
            <PageState state="loading" title="正在读取日线批次" description="正在加载最近交易日数据。" />
          ) : dailyBatches.isError ? (
            <SectionError error={dailyBatches.error} retry={() => void dailyBatches.refetch()} label="日线批次" />
          ) : dailyBatches.data.items.length === 0 ? (
            <PageState state="empty" title="还没有日线批次" description="当前没有可展示的全市场日线任务。" />
          ) : (
            <div className="overflow-x-auto border">
              <table className="w-full min-w-[600px] text-sm">
                <thead className="bg-muted/50 text-left text-xs text-muted-foreground">
                  <tr>
                    <th className="px-3 py-2 font-medium">交易日</th>
                    <th className="px-3 py-2 font-medium">状态</th>
                    <th className="px-3 py-2 text-right font-medium">抓取</th>
                    <th className="px-3 py-2 text-right font-medium">入库</th>
                    <th className="px-3 py-2 text-right font-medium">缺失 / 失败</th>
                  </tr>
                </thead>
                <tbody>
                  {dailyBatches.data.items.slice(0, 8).map((batch) => (
                    <tr key={batch.id} className="border-t">
                      <td className="px-3 py-2.5 font-medium">{batch.tradingDate}</td>
                      <td className="px-3 py-2.5"><Status value={batch.status} /></td>
                      <td className="px-3 py-2.5 text-right tabular-nums">{batch.fetchedCount}/{batch.expectedCount}</td>
                      <td className="px-3 py-2.5 text-right tabular-nums">{batch.committedCount}</td>
                      <td className="px-3 py-2.5 text-right tabular-nums">{batch.missingCount} / {batch.failedCount}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <Panel
          title="前复权数据"
          description="按股票核对当前生效数据集、覆盖窗口和新鲜度"
          icon={SearchIcon}
        >
          <form
            className="mb-3 flex gap-2"
            onSubmit={(event) => {
              event.preventDefault()
              const normalized = qfqInput.trim().toUpperCase()
              if (normalized) setQfqSymbol(normalized)
            }}
          >
            <Input
              aria-label="股票代码"
              placeholder="例如 600519.SH"
              value={qfqInput}
              onChange={(event) => setQfqInput(event.target.value)}
            />
            <Button type="submit" variant="outline" disabled={!qfqInput.trim() || qfq.isFetching}>
              <SearchIcon data-icon="inline-start" />
              查询
            </Button>
          </form>
          {!qfqSymbol ? (
            <PageState state="empty" title="输入股票代码开始查询" description="只读取当前生效的前复权数据集，不会触发刷新。" />
          ) : qfq.isPending ? (
            <PageState state="loading" title="正在读取前复权数据" description={`正在查询 ${qfqSymbol}。`} />
          ) : qfq.isError ? (
            <SectionError error={qfq.error} retry={() => void qfq.refetch()} label="前复权数据" />
          ) : (
            <dl className="grid grid-cols-2 gap-px border bg-border text-sm sm:grid-cols-3">
              {[
                ["股票", qfq.data.symbol],
                ["数据版本", `v${qfq.data.version}`],
                ["状态", `${statusLabels[qfq.data.lifecycle] ?? qfq.data.lifecycle} / ${statusLabels[qfq.data.freshness] ?? qfq.data.freshness}`],
                ["覆盖区间", `${qfq.data.actualStart} 至 ${qfq.data.actualEnd}`],
                ["数据源", qfq.data.provider],
                ["记录数", qfq.data.rowCount.toLocaleString("zh-CN")],
              ].map(([label, value]) => (
                <div key={label} className="min-w-0 bg-background p-3">
                  <dt className="text-xs text-muted-foreground">{label}</dt>
                  <dd className="mt-1 break-words font-medium">{value}</dd>
                </div>
              ))}
            </dl>
          )}
        </Panel>

        <Panel
          title="数据质量问题"
          description="冲突、缺失和异常数据的待处理记录"
          icon={AlertTriangleIcon}
        >
          {qualityIssues.isPending ? (
            <PageState state="loading" title="正在读取质量问题" description="正在加载异常记录。" />
          ) : qualityIssues.isError ? (
            <SectionError error={qualityIssues.error} retry={() => void qualityIssues.refetch()} label="质量问题" />
          ) : qualityIssues.data.items.length === 0 ? (
            <PageState state="empty" title="没有数据质量问题" description="当前没有需要人工关注的异常记录。" />
          ) : (
            <div className="space-y-2">
              {qualityIssues.data.items.slice(0, 8).map((issue) => (
                <div key={issue.id} className="grid gap-3 border px-3 py-2.5 sm:grid-cols-[1fr_auto]">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium">
                      {issue.symbol ?? issue.subjectType} · {issueTypeLabels[issue.issueType] ?? issue.issueType}
                    </p>
                    <p className="text-xs text-muted-foreground">
                      最近 {dateTime(issue.lastSeenAt)} · 累计 {issue.occurrenceCount} 次
                      {issue.selectedSource ? ` · 已选来源 ${issue.selectedSource}` : ""}
                    </p>
                  </div>
                  <div className="flex flex-wrap items-center justify-end gap-2">
                    {issue.allowedActions.includes("SELECT_SOURCE") ? (
                      <Button size="sm" variant="outline" onClick={() => openQualityCommand(issue, "SELECT_SOURCE")}>
                        选择来源
                      </Button>
                    ) : null}
                    {issue.allowedActions.includes("REFETCH") ? (
                      <Button size="sm" variant="outline" onClick={() => openQualityCommand(issue, "REFETCH")}>
                        重新抓取
                      </Button>
                    ) : null}
                    {issue.allowedActions.includes("INVALIDATE") ? (
                      <Button size="sm" variant="ghost" onClick={() => openQualityCommand(issue, "INVALIDATE")}>
                        标为无效
                      </Button>
                    ) : null}
                    <Status value={issue.severity} />
                    <Status value={issue.status} />
                  </div>
                </div>
              ))}
              <p className="text-xs text-muted-foreground">只能选择系统已有来源或标记无效，页面不接受人工输入价格。</p>
            </div>
          )}
        </Panel>

        <Panel
          title="历史回填"
          description="全市场或指定范围的历史日线补齐进度"
          icon={HistoryIcon}
        >
          {backfills.isPending ? (
            <PageState state="loading" title="正在读取历史回填" description="正在加载任务进度。" />
          ) : backfills.isError ? (
            <SectionError error={backfills.error} retry={() => void backfills.refetch()} label="历史回填" />
          ) : backfills.data.items.length === 0 ? (
            <PageState state="empty" title="还没有历史回填任务" description="当前没有可展示的回填记录。" />
          ) : (
            <div className="space-y-2">
              {backfills.data.items.slice(0, 8).map((job) => {
                const percentage = job.total > 0
                  ? Math.min(100, Math.round((job.completed / job.total) * 100))
                  : 0
                return (
                  <div key={job.id} className="border px-3 py-2.5">
                    <div className="flex items-center justify-between gap-3">
                      <span className="truncate text-sm font-medium">{job.id}</span>
                      <Status value={job.status} />
                    </div>
                    <div className="mt-2 h-1.5 overflow-hidden bg-muted" aria-label={`完成 ${percentage}%`}>
                      <div className="h-full bg-foreground" style={{ width: `${percentage}%` }} />
                    </div>
                    <p className="mt-1.5 text-xs text-muted-foreground">
                      {job.completed}/{job.total} · 成功 {job.succeeded ?? "—"} · 失败 {job.failed ?? "—"} · 更新于 {dateTime(job.updatedAt)}
                    </p>
                  </div>
                )
              })}
            </div>
          )}
        </Panel>
      </div>
      <Dialog
        open={qualityCommand !== null}
        onOpenChange={(open) => {
          if (!open && !qualityMutation.isPending) setQualityCommand(null)
        }}
      >
        <DialogContent>
          <DialogTitle>
            {qualityCommand?.action === "SELECT_SOURCE"
              ? "选择可信来源"
              : qualityCommand?.action === "REFETCH"
                ? "重新抓取数据"
                : "标记数据无效"}
          </DialogTitle>
          <DialogDescription>
            {qualityCommand
              ? `${qualityCommand.issue.symbol ?? qualityCommand.issue.subjectType} · ${issueTypeLabels[qualityCommand.issue.issueType] ?? qualityCommand.issue.issueType}`
              : ""}
          </DialogDescription>
          {qualityCommand?.action === "SELECT_SOURCE" ? (
            <label className="grid gap-2 text-sm font-medium">
              数据来源
              <select
                className="h-9 w-full rounded-md border bg-background px-3 text-sm"
                value={selectedSource}
                onChange={(event) => setSelectedSource(event.target.value)}
              >
                {qualityCommand.issue.sourceCandidates.map((source) => (
                  <option key={source} value={source}>{source}</option>
                ))}
              </select>
            </label>
          ) : null}
          <label className="grid gap-2 text-sm font-medium">
            操作原因
            <Input
              value={qualityReason}
              maxLength={500}
              placeholder="请说明本次处置原因"
              onChange={(event) => setQualityReason(event.target.value)}
            />
          </label>
          {qualityMutation.isError ? (
            <p role="alert" className="text-sm text-destructive">
              处置未完成，请核对当前状态后重试。
              {qualityMutation.error instanceof ApiError
                ? ` 错误码：${qualityMutation.error.code}`
                : ""}
            </p>
          ) : null}
          <DialogFooter>
            <Button
              variant="outline"
              disabled={qualityMutation.isPending}
              onClick={() => setQualityCommand(null)}
            >
              取消
            </Button>
            <Button
              disabled={
                qualityMutation.isPending
                || !qualityReason.trim()
                || (qualityCommand?.action === "SELECT_SOURCE" && !selectedSource)
              }
              onClick={() => qualityMutation.mutate()}
            >
              {qualityMutation.isPending ? "正在提交" : "确认处置"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </main>
  )
}
