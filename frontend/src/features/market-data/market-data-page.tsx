import { useState, type ReactNode } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  ActivityIcon,
  AlertTriangleIcon,
  CalendarRangeIcon,
  ChevronRightIcon,
  DatabaseIcon,
  HistoryIcon,
  PauseIcon,
  PlayIcon,
  RefreshCwIcon,
  RotateCcwIcon,
  SearchIcon,
  ShieldCheckIcon,
  XIcon,
} from "lucide-react"

import { marketDataGateway } from "@/features/market-data/gateway"
import type {
  MarketDataGateway,
  BackfillAction,
  BackfillSummary,
  DailyBatchSummary,
  QfqDatasetSummary,
  QuoteOperationAction,
  QualityIssueAction,
  QualityIssueSummary,
} from "@/features/market-data/types"
import { ApiError } from "@/shared/api/client"
import { Alert, AlertDescription } from "@/shared/ui/alert"
import { Badge } from "@/shared/ui/badge"
import { Button } from "@/shared/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/shared/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogTitle,
} from "@/shared/ui/dialog"
import { Input } from "@/shared/ui/input"
import { NativeSelect, NativeSelectOption } from "@/shared/ui/native-select"
import { PageState } from "@/shared/ui/page-state"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/shared/ui/table"

interface MarketDataPageProps {
  gateway?: MarketDataGateway
}

type MarketCommand =
  | { kind: "SECURITY_REFRESH" }
  | { kind: "QUOTE"; action: QuoteOperationAction }
  | { kind: "DAILY_RETRY"; batch: DailyBatchSummary }
  | { kind: "QFQ_REFRESH"; dataset: QfqDatasetSummary }
  | { kind: "BACKFILL_CREATE" }
  | {
    kind: "BACKFILL_CONTROL"
    action: Exclude<BackfillAction, "CREATE">
    job: BackfillSummary
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

function normalizedSymbols(value: string) {
  return [...new Set(
    value
      .split(/[\s,，]+/)
      .map((item) => item.trim().toUpperCase())
      .filter(Boolean),
  )]
}

function statusVariant(status: string): "default" | "destructive" | "secondary" | "outline" {
  if (["READY", "COMPLETED", "SUCCEEDED", "CURRENT", "FRESH", "RESOLVED"].includes(status)) {
    return "default"
  }
  if (["FAILED", "CONFLICT", "CRITICAL", "STALE"].includes(status)) {
    return "destructive"
  }
  if (["PARTIAL", "WARNING", "PAUSED", "OPEN"].includes(status)) {
    return "secondary"
  }
  return "outline"
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
    <Badge variant={statusVariant(value)}>
      {statusLabels[value] ?? value}
    </Badge>
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
    <Card className="min-w-0">
      <CardHeader className="grid-cols-[auto_1fr]">
        <div className="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded border bg-muted/50">
          <Icon className="size-4" aria-hidden="true" />
        </div>
        <div className="min-w-0">
          <CardTitle>{title}</CardTitle>
          <CardDescription>{description}</CardDescription>
        </div>
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
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
  const [marketCommand, setMarketCommand] = useState<MarketCommand | null>(null)
  const [commandReason, setCommandReason] = useState("")
  const [commandSymbols, setCommandSymbols] = useState("")
  const [quoteTimeout, setQuoteTimeout] = useState(30)
  const [backfillScope, setBackfillScope] = useState<
    "SINGLE" | "SELECTED" | "ALL"
  >("SINGLE")
  const [backfillStart, setBackfillStart] = useState("")
  const [backfillEnd, setBackfillEnd] = useState("")
  const [backfillConcurrency, setBackfillConcurrency] = useState(4)

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
  const marketMutation = useMutation({
    mutationFn: async () => {
      if (!marketCommand) return
      if (marketCommand.kind === "SECURITY_REFRESH") {
        return gateway.refreshSecurities(commandReason.trim())
      }
      if (marketCommand.kind === "QUOTE") {
        return gateway.runQuoteOperation({
          action: marketCommand.action,
          symbols: normalizedSymbols(commandSymbols),
          timeoutSeconds: quoteTimeout,
          reason: commandReason.trim(),
        })
      }
      if (marketCommand.kind === "DAILY_RETRY") {
        return gateway.retryDailyBatch({
          batchId: marketCommand.batch.id,
          reason: commandReason.trim(),
        })
      }
      if (marketCommand.kind === "QFQ_REFRESH") {
        return gateway.refreshQfq({
          dataset: marketCommand.dataset,
          reason: commandReason.trim(),
        })
      }
      if (marketCommand.kind === "BACKFILL_CREATE") {
        return gateway.createBackfill({
          scope: backfillScope,
          symbols: backfillScope === "ALL"
            ? []
            : normalizedSymbols(commandSymbols),
          startDate: backfillStart,
          endDate: backfillEnd,
          concurrency: backfillConcurrency,
          reason: commandReason.trim(),
        })
      }
      return gateway.runBackfillAction({
        job: marketCommand.job,
        action: marketCommand.action,
        reason: commandReason.trim(),
      })
    },
    onSuccess: async () => {
      const command = marketCommand
      setMarketCommand(null)
      if (!command) return
      const query = command.kind === "SECURITY_REFRESH"
        ? "securities"
        : command.kind === "QUOTE"
          ? "quote-cycles"
          : command.kind === "DAILY_RETRY"
            ? "daily-batches"
            : command.kind === "QFQ_REFRESH"
              ? "qfq"
              : "backfills"
      await queryClient.invalidateQueries({
        queryKey: ["market-data", query],
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

  function openMarketCommand(command: MarketCommand) {
    setMarketCommand(command)
    setCommandReason("")
    setCommandSymbols("")
    setQuoteTimeout(30)
    marketMutation.reset()
  }

  const commandNeedsSymbols = marketCommand?.kind === "QUOTE"
    || (
      marketCommand?.kind === "BACKFILL_CREATE"
      && backfillScope !== "ALL"
    )
  const commandInvalid = !commandReason.trim()
    || (commandNeedsSymbols && normalizedSymbols(commandSymbols).length === 0)
    || (
      marketCommand?.kind === "BACKFILL_CREATE"
      && backfillScope === "SINGLE"
      && normalizedSymbols(commandSymbols).length !== 1
    )
    || (
      marketCommand?.kind === "BACKFILL_CREATE"
      && (!backfillStart || !backfillEnd || backfillStart > backfillEnd)
    )

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
          {securities.data?.allowedActions.includes("REFRESH") ? (
            <div className="mb-3 flex justify-end">
              <Button
                size="sm"
                variant="outline"
                onClick={() => openMarketCommand({
                  kind: "SECURITY_REFRESH",
                })}
              >
                <RefreshCwIcon data-icon="inline-start" />
                刷新主数据
              </Button>
            </div>
          ) : null}
          {securities.isPending ? (
            <PageState state="loading" title="正在读取证券主数据" description="正在加载最新主数据版本。" />
          ) : securities.isError ? (
            <SectionError error={securities.error} retry={() => void securities.refetch()} label="证券主数据" />
          ) : securities.data.items.length === 0 ? (
            <PageState state="empty" title="还没有证券主数据" description="系统尚未形成可展示的证券清单。" />
          ) : (
            <>
              <div className="overflow-x-auto border">
              <Table className="min-w-[620px]">
                <TableHeader>
                  <TableRow>
                    <TableHead>代码 / 名称</TableHead>
                    <TableHead>市场</TableHead>
                    <TableHead>状态</TableHead>
                    <TableHead className="text-right">版本</TableHead>
                    <TableHead className="text-right">更新时间</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {securities.data.items.slice(0, 8).map((item) => (
                    <TableRow key={item.id}>
                      <TableCell>
                        <strong className="font-medium">{item.symbol}</strong>
                        <span className="ml-2 text-muted-foreground">{item.name}</span>
                        {item.isSt || item.isSuspended ? (
                          <AlertTriangleIcon className="ml-2 inline size-3.5 text-amber-700" aria-label="存在特别状态" />
                        ) : null}
                      </TableCell>
                      <TableCell>{item.market}</TableCell>
                      <TableCell><Status value={item.listingStatus} /></TableCell>
                      <TableCell className="text-right tabular-nums">v{item.masterVersion}</TableCell>
                      <TableCell className="text-right text-muted-foreground">{dateTime(item.updatedAt)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
              <p className="border-t px-3 py-2 text-xs text-muted-foreground">
                共 {securities.data.pagination.total} 只，当前显示前 {Math.min(8, securities.data.items.length)} 只
              </p>
              </div>
            </>
          )}
        </Panel>

        <Panel
          title="实时采集周期"
          description="批次屏障状态及逐股有效性诊断"
          icon={ActivityIcon}
        >
          {quoteCycles.data ? (
            <div className="mb-3 flex flex-wrap justify-end gap-2">
              {quoteCycles.data.allowedActions.includes("MANUAL_COLLECT") ? (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => openMarketCommand({
                    kind: "QUOTE",
                    action: "MANUAL_COLLECT",
                  })}
                >
                  <PlayIcon data-icon="inline-start" />
                  手动采集
                </Button>
              ) : null}
              {quoteCycles.data.allowedActions.includes("DIAGNOSE") ? (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => openMarketCommand({
                    kind: "QUOTE",
                    action: "DIAGNOSE",
                  })}
                >
                  <ActivityIcon data-icon="inline-start" />
                  行情诊断
                </Button>
              ) : null}
            </div>
          ) : null}
          {quoteCycles.isPending ? (
            <PageState state="loading" title="正在读取实时采集周期" description="正在加载最近批次。" />
          ) : quoteCycles.isError ? (
            <SectionError error={quoteCycles.error} retry={() => void quoteCycles.refetch()} label="实时采集周期" />
          ) : quoteCycles.data.items.length === 0 ? (
            <PageState state="empty" title="还没有实时采集周期" description="当前没有可展示的采集批次。" />
          ) : (
            <div className="space-y-2">
              {quoteCycles.data.items.slice(0, 5).map((cycle) => (
                <Button
                  type="button"
                  key={cycle.id}
                  variant="outline"
                  className="h-auto w-full justify-start whitespace-normal px-3 py-2.5 text-left"
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
                </Button>
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
                    <Alert variant="destructive"><AlertDescription>批次明细读取失败，主列表不受影响。</AlertDescription></Alert>
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
              <Table className="min-w-[600px]">
                <TableHeader>
                  <TableRow>
                    <TableHead>交易日</TableHead>
                    <TableHead>状态</TableHead>
                    <TableHead className="text-right">抓取</TableHead>
                    <TableHead className="text-right">入库</TableHead>
                    <TableHead className="text-right">缺失 / 失败</TableHead>
                    <TableHead className="text-right">操作</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {dailyBatches.data.items.slice(0, 8).map((batch) => (
                    <TableRow key={batch.id}>
                      <TableCell className="font-medium">{batch.tradingDate}</TableCell>
                      <TableCell><Status value={batch.status} /></TableCell>
                      <TableCell className="text-right tabular-nums">{batch.fetchedCount}/{batch.expectedCount}</TableCell>
                      <TableCell className="text-right tabular-nums">{batch.committedCount}</TableCell>
                      <TableCell className="text-right tabular-nums">{batch.missingCount} / {batch.failedCount}</TableCell>
                      <TableCell className="text-right">
                        {batch.allowedActions.includes("RETRY_MISSING") ? (
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => openMarketCommand({
                              kind: "DAILY_RETRY",
                              batch,
                            })}
                          >
                            <RotateCcwIcon data-icon="inline-start" />
                            重试缺失
                          </Button>
                        ) : (
                          <span className="text-xs text-muted-foreground">—</span>
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
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
            <div>
              {qfq.data.allowedActions.includes("REFRESH") ? (
                <div className="mb-3 flex justify-end">
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => openMarketCommand({
                      kind: "QFQ_REFRESH",
                      dataset: qfq.data,
                    })}
                  >
                    <RefreshCwIcon data-icon="inline-start" />
                    刷新前复权
                  </Button>
                </div>
              ) : null}
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
            </div>
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
          {backfills.data?.allowedActions.includes("CREATE") ? (
            <div className="mb-3 flex justify-end">
              <Button
                size="sm"
                onClick={() => openMarketCommand({ kind: "BACKFILL_CREATE" })}
              >
                <PlayIcon data-icon="inline-start" />
                新建回填
              </Button>
            </div>
          ) : null}
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
                    {job.allowedActions.length > 0 ? (
                      <div className="mt-2 flex flex-wrap justify-end gap-2 border-t pt-2">
                        {job.allowedActions.map((action) => (
                          <Button
                            key={action}
                            size="sm"
                            variant={action === "CANCEL" ? "ghost" : "outline"}
                            onClick={() => openMarketCommand({
                              kind: "BACKFILL_CONTROL",
                              action,
                              job,
                            })}
                          >
                            {action === "PAUSE" ? (
                              <PauseIcon data-icon="inline-start" />
                            ) : action === "CANCEL" ? (
                              <XIcon data-icon="inline-start" />
                            ) : action === "RESUME" ? (
                              <PlayIcon data-icon="inline-start" />
                            ) : (
                              <RotateCcwIcon data-icon="inline-start" />
                            )}
                            {{
                              PAUSE: "暂停",
                              RESUME: "继续",
                              CANCEL: "取消",
                              RETRY_FAILED: "重试失败项",
                            }[action]}
                          </Button>
                        ))}
                      </div>
                    ) : null}
                  </div>
                )
              })}
            </div>
          )}
        </Panel>
      </div>
      <Dialog
        open={marketCommand !== null}
        onOpenChange={(open) => {
          if (!open && !marketMutation.isPending) setMarketCommand(null)
        }}
      >
        <DialogContent>
          <DialogTitle>
            {marketCommand?.kind === "SECURITY_REFRESH"
              ? "刷新证券主数据"
              : marketCommand?.kind === "QUOTE"
                ? marketCommand.action === "MANUAL_COLLECT"
                  ? "手动采集实时行情"
                  : "诊断实时行情"
                : marketCommand?.kind === "DAILY_RETRY"
                  ? "重试日线缺失项"
                  : marketCommand?.kind === "QFQ_REFRESH"
                    ? "刷新前复权数据"
                    : marketCommand?.kind === "BACKFILL_CREATE"
                      ? "新建历史回填"
                      : marketCommand?.kind === "BACKFILL_CONTROL"
                        ? {
                          PAUSE: "暂停历史回填",
                          RESUME: "继续历史回填",
                          CANCEL: "取消历史回填",
                          RETRY_FAILED: "重试失败股票",
                        }[marketCommand.action]
                        : ""}
          </DialogTitle>
          <DialogDescription>
            操作将创建后台任务。确认后请在当前区域查看最新状态。
          </DialogDescription>
          {marketCommand?.kind === "QUOTE" ? (
            <>
              <label className="grid gap-2 text-sm font-medium">
                股票代码
                <Input
                  value={commandSymbols}
                  placeholder="多个代码用逗号或空格分隔"
                  onChange={(event) => setCommandSymbols(event.target.value)}
                />
              </label>
              {marketCommand.action === "MANUAL_COLLECT" ? (
                <label className="grid gap-2 text-sm font-medium">
                  截止时间（秒）
                  <Input
                    type="number"
                    min={10}
                    max={60}
                    value={quoteTimeout}
                    onChange={(event) => setQuoteTimeout(
                      Math.min(60, Math.max(10, Number(event.target.value))),
                    )}
                  />
                </label>
              ) : null}
            </>
          ) : null}
          {marketCommand?.kind === "DAILY_RETRY" ? (
            <Alert><AlertDescription>
              {marketCommand.batch.tradingDate} · 缺失{" "}
              {marketCommand.batch.missingCount} · 失败{" "}
              {marketCommand.batch.failedCount}
            </AlertDescription></Alert>
          ) : null}
          {marketCommand?.kind === "QFQ_REFRESH" ? (
            <Alert><AlertDescription>
              {marketCommand.dataset.symbol} ·{" "}
              {marketCommand.dataset.actualStart} 至{" "}
              {marketCommand.dataset.actualEnd} · 当前版本 v
              {marketCommand.dataset.version}
            </AlertDescription></Alert>
          ) : null}
          {marketCommand?.kind === "BACKFILL_CREATE" ? (
            <>
              <label className="grid gap-2 text-sm font-medium">
                回填范围
                <NativeSelect
                  className="w-full"
                  value={backfillScope}
                  onChange={(event) => setBackfillScope(
                    event.target.value as typeof backfillScope,
                  )}
                >
                  <NativeSelectOption value="SINGLE">单只股票</NativeSelectOption>
                  <NativeSelectOption value="SELECTED">选择多只股票</NativeSelectOption>
                  <NativeSelectOption value="ALL">全部股票</NativeSelectOption>
                </NativeSelect>
              </label>
              {backfillScope !== "ALL" ? (
                <label className="grid gap-2 text-sm font-medium">
                  股票代码
                  <Input
                    value={commandSymbols}
                    placeholder={backfillScope === "SINGLE"
                      ? "例如 600519.SH"
                      : "多个代码用逗号或空格分隔"}
                    onChange={(event) => setCommandSymbols(event.target.value)}
                  />
                </label>
              ) : null}
              <div className="grid grid-cols-2 gap-3">
                <label className="grid gap-2 text-sm font-medium">
                  开始日期
                  <Input
                    type="date"
                    value={backfillStart}
                    onChange={(event) => setBackfillStart(event.target.value)}
                  />
                </label>
                <label className="grid gap-2 text-sm font-medium">
                  结束日期
                  <Input
                    type="date"
                    value={backfillEnd}
                    onChange={(event) => setBackfillEnd(event.target.value)}
                  />
                </label>
              </div>
              <label className="grid gap-2 text-sm font-medium">
                并发数
                <Input
                  type="number"
                  min={1}
                  max={8}
                  value={backfillConcurrency}
                  onChange={(event) => setBackfillConcurrency(
                    Math.min(8, Math.max(1, Number(event.target.value))),
                  )}
                />
              </label>
            </>
          ) : null}
          {marketCommand?.kind === "BACKFILL_CONTROL" ? (
            <Alert><AlertDescription>
              任务 {marketCommand.job.id} · 当前状态{" "}
              {statusLabels[marketCommand.job.status] ?? marketCommand.job.status}
              {" "}· 版本 v{marketCommand.job.version}
            </AlertDescription></Alert>
          ) : null}
          <label className="grid gap-2 text-sm font-medium">
            操作原因
            <Input
              value={commandReason}
              maxLength={marketCommand?.kind === "QFQ_REFRESH" ? 64 : 500}
              placeholder="请说明本次操作原因"
              onChange={(event) => setCommandReason(event.target.value)}
            />
          </label>
          {marketMutation.isError ? (
            <p role="alert" className="text-sm text-destructive">
              操作未受理，请刷新当前区域后核对状态。
              {marketMutation.error instanceof ApiError
                ? ` 错误码：${marketMutation.error.code}`
                : ""}
            </p>
          ) : null}
          <DialogFooter>
            <Button
              variant="outline"
              disabled={marketMutation.isPending}
              onClick={() => setMarketCommand(null)}
            >
              取消
            </Button>
            <Button
              disabled={marketMutation.isPending || commandInvalid}
              onClick={() => marketMutation.mutate()}
            >
              {marketMutation.isPending ? "正在提交" : "确认执行"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
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
              <NativeSelect
                className="w-full"
                value={selectedSource}
                onChange={(event) => setSelectedSource(event.target.value)}
              >
                {qualityCommand.issue.sourceCandidates.map((source) => (
                  <NativeSelectOption key={source} value={source}>{source}</NativeSelectOption>
                ))}
              </NativeSelect>
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
