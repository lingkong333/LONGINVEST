import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  Calculator,
  CheckCircle2,
  Clock3,
  History,
  ListFilter,
  PencilLine,
  RefreshCw,
  RotateCcw,
  ShieldAlert,
  XCircle,
} from "lucide-react"
import { useMemo, useState } from "react"

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

import type {
  CalculateTargetInput,
  ManualTargetInput,
  TargetAction,
  TargetItem,
  TargetManagementApi,
  TargetReviewItem,
  TargetRevision,
  TargetValues,
} from "./types"

type View = "overview" | "detail" | "history" | "runs" | "reviews"
type Operation =
  | { kind: "manual"; target: TargetItem }
  | { kind: "calculate"; target: TargetItem }
  | { kind: "restore"; target: TargetItem; revision: TargetRevision }
  | { kind: "review"; decision: "approve" | "reject" | "recalculate"; review: TargetReviewItem }

const valueFields: Array<{ key: keyof TargetValues; label: string }> = [
  { key: "low_strong", label: "强低吸价" },
  { key: "low_watch", label: "低位观察价" },
  { key: "high_watch", label: "高位观察价" },
  { key: "high_strong", label: "强高抛价" },
]

const statusLabels: Record<string, string> = {
  READY: "可用",
  STALE: "已过期",
  CALCULATING: "计算中",
  REVIEW_REQUIRED: "待复核",
  ACTIVATING: "激活中",
  FAILED: "失败",
  MISSING: "缺少目标",
  PENDING: "等待中",
  RUNNING: "运行中",
  SUCCEEDED: "成功",
  APPROVED: "已通过",
  REJECTED: "已驳回",
  SUPERSEDED: "已失效",
}

const sourceLabels: Record<string, string> = {
  MANUAL: "手工",
  STRATEGY: "策略",
  RESTORED: "历史恢复",
  DATA_CORRECTION: "数据修正",
  STRATEGY_CHANGE: "策略变更",
  PARAMETER_CHANGE: "参数变更",
}

function formatDate(value: string | null | undefined, withTime = false) {
  if (!value) return "—"
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    ...(withTime ? { hour: "2-digit", minute: "2-digit" } : {}),
  }).format(new Date(value))
}

function hasAction(actions: TargetAction[], action: TargetAction) {
  return actions.includes(action)
}

function ValuesStrip({ values, compare }: { values: TargetValues; compare?: TargetValues }) {
  return (
    <dl className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border bg-border lg:grid-cols-4">
      {valueFields.map(({ key, label }) => {
        const previous = compare ? Number(compare[key]) : null
        const current = Number(values[key])
        const change = previous && Number.isFinite(current)
          ? ((current - previous) / Math.max(Math.abs(previous), 0.01)) * 100
          : null
        return (
          <div key={key} className="bg-card px-4 py-3">
            <dt className="text-xs text-muted-foreground">{label}</dt>
            <dd className="mt-1 font-mono text-lg font-semibold">¥ {values[key]}</dd>
            {compare ? (
              <p className="mt-1 text-xs text-muted-foreground">
                原 ¥ {compare[key]} · {change === null ? "—" : `${change >= 0 ? "+" : ""}${change.toFixed(1)}%`}
              </p>
            ) : null}
          </div>
        )
      })}
    </dl>
  )
}

function TargetCard({ item, selected, onSelect }: {
  item: TargetItem
  selected: boolean
  onSelect: () => void
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className="w-full rounded-lg border bg-card p-4 text-left transition hover:border-primary/50 aria-pressed:border-primary aria-pressed:ring-2 aria-pressed:ring-primary/10"
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="font-mono text-sm font-semibold">{item.subscription_id.slice(0, 8)}</p>
          <p className="mt-1 text-xs text-muted-foreground">目标日期 {formatDate(item.target_date)}</p>
        </div>
        <span className="rounded-full bg-muted px-2 py-1 text-xs font-medium">
          {statusLabels[item.status] ?? item.status}
        </span>
      </div>
      <div className="mt-4 flex items-end justify-between">
        <div>
          <p className="text-xs text-muted-foreground">当前价格区间</p>
          <p className="mt-1 font-mono text-sm">¥ {item.values.low_strong} — {item.values.high_strong}</p>
        </div>
        <span className="text-xs text-muted-foreground">{sourceLabels[item.source] ?? item.source}</span>
      </div>
    </button>
  )
}

function ErrorState({ error, retry }: { error: unknown; retry: () => void }) {
  const apiError = error instanceof ApiError ? error : null
  return (
    <PageState
      state={apiError?.status === 409 ? "conflict" : "error"}
      title={apiError?.status === 409 ? "数据已发生变化" : "目标数据无法加载"}
      description={apiError?.status === 409 ? "其他操作已更新数据，请重新加载后再处理。" : "请检查网络后重试。"}
      error={apiError ? { code: apiError.code, requestId: apiError.requestId } : undefined}
      action={{ label: "重新加载", onClick: retry }}
    />
  )
}

export function TargetManagementPage({ api }: { api: TargetManagementApi }) {
  const queryClient = useQueryClient()
  const [view, setView] = useState<View>("overview")
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [operation, setOperation] = useState<Operation | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const targets = useQuery({ queryKey: ["targets"], queryFn: api.listTargets })
  const selected = targets.data?.find((item) => item.subscription_id === selectedId) ?? targets.data?.[0]
  const history = useQuery({
    queryKey: ["targets", selected?.subscription_id, "history"],
    queryFn: () => api.listHistory(selected!.subscription_id),
    enabled: Boolean(selected),
  })
  const runs = useQuery({ queryKey: ["target-runs"], queryFn: api.listRuns, enabled: view === "runs" })
  const reviews = useQuery({ queryKey: ["target-reviews"], queryFn: api.listReviews, enabled: view === "reviews" })

  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["targets"] }),
      queryClient.invalidateQueries({ queryKey: ["target-runs"] }),
      queryClient.invalidateQueries({ queryKey: ["target-reviews"] }),
    ])
  }

  if (targets.isPending) {
    return <PageState state="loading" title="正在加载目标" description="正在获取最新目标和状态。" />
  }
  if (targets.isError) return <ErrorState error={targets.error} retry={() => void targets.refetch()} />

  const openTarget = (item: TargetItem) => {
    setSelectedId(item.subscription_id)
    setView("detail")
  }

  return (
    <main className="mx-auto w-full max-w-[92rem] px-4 py-6 sm:px-6 lg:px-8">
      <header className="flex flex-col gap-4 border-b pb-5 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <p className="text-xs font-semibold tracking-[0.18em] text-primary">价格决策台</p>
          <h1 className="mt-2 text-2xl font-semibold tracking-tight">目标管理</h1>
          <p className="mt-1 text-sm text-muted-foreground">查看四档目标、计算记录与待复核变化。</p>
        </div>
        <Button variant="outline" onClick={() => void refresh()}><RefreshCw />刷新</Button>
      </header>

      <nav aria-label="目标管理视图" className="mt-5 flex gap-1 overflow-x-auto rounded-lg border bg-card p-1">
        {([
          ["overview", "总览", ListFilter],
          ["detail", "股票详情", PencilLine],
          ["history", "版本历史", History],
          ["runs", "计算运行", Calculator],
          ["reviews", "待复核", ShieldAlert],
        ] as const).map(([key, label, Icon]) => (
          <Button
            key={key}
            type="button"
            variant={view === key ? "secondary" : "ghost"}
            onClick={() => setView(key)}
            className="flex-1"
          >
            <Icon />{label}
          </Button>
        ))}
      </nav>

      {notice ? <p role="status" className="mt-4 rounded-lg border border-primary/20 bg-accent px-4 py-3 text-sm">{notice}</p> : null}

      {view === "overview" ? (
        targets.data.length === 0
          ? <PageState state="empty" title="暂无目标" description="订阅完成目标计算后会显示在这里。" />
          : (
            <section className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-3" aria-label="目标总览">
              {targets.data.map((item) => (
                <TargetCard key={item.subscription_id} item={item} selected={item.subscription_id === selected?.subscription_id} onSelect={() => openTarget(item)} />
              ))}
            </section>
          )
      ) : null}

      {view === "detail" ? (
        selected
          ? <TargetDetail item={selected} onOperation={setOperation} />
          : <PageState state="empty" title="尚未选择股票" description="请先从总览中选择一个目标。" />
      ) : null}

      {view === "history" ? (
        selected
          ? <HistoryView item={selected} history={history.data ?? []} loading={history.isPending} error={history.error} onRetry={() => void history.refetch()} onRestore={(revision) => setOperation({ kind: "restore", target: selected, revision })} />
          : <PageState state="empty" title="尚未选择股票" description="请先从总览中选择一个目标。" />
      ) : null}

      {view === "runs" ? <RunsView query={runs} /> : null}
      {view === "reviews" ? <ReviewsView query={reviews} onReview={(decision, review) => setOperation({ kind: "review", decision, review })} /> : null}

      <OperationDialog
        operation={operation}
        api={api}
        onClose={() => setOperation(null)}
        onDone={async (message) => {
          setOperation(null)
          setNotice(message)
          await refresh()
        }}
      />
    </main>
  )
}

function TargetDetail({ item, onOperation }: { item: TargetItem; onOperation: (operation: Operation) => void }) {
  const hasCapabilities = item.allowedActions.length > 0
  return (
    <section className="mt-5 space-y-5" aria-label="股票目标详情">
      <div className="rounded-xl border bg-card p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <p className="font-mono text-sm text-muted-foreground">订阅 {item.subscription_id}</p>
            <h2 className="mt-1 text-xl font-semibold">第 {item.revision_no} 版目标</h2>
          </div>
          <div className="text-right text-sm">
            <p>{statusLabels[item.status] ?? item.status}</p>
            <p className="mt-1 text-muted-foreground">{sourceLabels[item.source] ?? item.source} · {formatDate(item.activated_at, true)}</p>
          </div>
        </div>
        <div className="mt-5"><ValuesStrip values={item.values} /></div>
        <dl className="mt-5 grid gap-3 text-sm sm:grid-cols-3">
          <div><dt className="text-muted-foreground">目标日期</dt><dd className="mt-1">{formatDate(item.target_date)}</dd></div>
          <div><dt className="text-muted-foreground">数据版本</dt><dd className="mt-1">{item.data_version ?? "—"}</dd></div>
          <div><dt className="text-muted-foreground">策略版本</dt><dd className="mt-1 break-all">{item.strategy_version_id ?? "不适用"}</dd></div>
        </dl>
      </div>
      {!hasCapabilities ? (
        <p className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">
          后端尚未返回本目标的允许操作，当前仅可查看，避免绕过权限和状态校验。
        </p>
      ) : null}
      <div className="flex flex-wrap gap-2">
        <Button disabled={!hasAction(item.allowedActions, "MANUAL_EDIT")} onClick={() => onOperation({ kind: "manual", target: item })}><PencilLine />手工编辑</Button>
        <Button variant="secondary" disabled={!hasAction(item.allowedActions, "CALCULATE")} onClick={() => onOperation({ kind: "calculate", target: item })}><Calculator />运行计算</Button>
      </div>
    </section>
  )
}

function HistoryView({ item, history, loading, error, onRetry, onRestore }: {
  item: TargetItem
  history: TargetRevision[]
  loading: boolean
  error: unknown
  onRetry: () => void
  onRestore: (revision: TargetRevision) => void
}) {
  if (loading) return <PageState state="loading" title="正在加载版本历史" description="正在读取不可变目标版本。" />
  if (error) return <ErrorState error={error} retry={onRetry} />
  if (history.length === 0) return <PageState state="empty" title="暂无历史版本" description="新的目标版本会保留在这里。" />
  return (
    <section className="mt-5 space-y-3" aria-label="目标版本历史">
      {history.map((revision) => (
        <article key={revision.id} className="rounded-lg border bg-card p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 className="font-semibold">第 {revision.revision_no} 版 · {sourceLabels[revision.source] ?? revision.source}</h2>
              <p className="mt-1 text-xs text-muted-foreground">{formatDate(revision.created_at, true)} · {revision.reason}</p>
            </div>
            <Button
              size="sm"
              variant="outline"
              disabled={revision.id === item.revision_id || !hasAction(item.allowedActions, "RESTORE")}
              onClick={() => onRestore(revision)}
            >
              <RotateCcw />恢复此版本
            </Button>
          </div>
          <div className="mt-4"><ValuesStrip values={revision.values} compare={item.values} /></div>
        </article>
      ))}
    </section>
  )
}

type QueryResult<T> = {
  data: T | undefined
  isPending: boolean
  error: unknown
  refetch: () => Promise<unknown>
}

function RunsView({ query }: { query: QueryResult<Awaited<ReturnType<TargetManagementApi["listRuns"]>>> }) {
  if (query.isPending) return <PageState state="loading" title="正在加载计算记录" description="正在读取目标计算运行情况。" />
  if (query.error) return <ErrorState error={query.error} retry={() => void query.refetch()} />
  if (!query.data?.length) return <PageState state="empty" title="暂无计算记录" description="策略目标开始计算后会显示在这里。" />
  return (
    <section className="mt-5 overflow-hidden rounded-lg border bg-card" aria-label="目标计算运行">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[48rem] text-left text-sm">
          <thead className="border-b bg-muted/60 text-xs text-muted-foreground">
            <tr><th className="p-3">创建时间</th><th>订阅</th><th>训练区间</th><th>状态</th><th>错误摘要</th></tr>
          </thead>
          <tbody>
            {query.data.map((run) => (
              <tr key={run.id} className="border-b last:border-0">
                <td className="p-3">{formatDate(run.created_at, true)}</td>
                <td className="font-mono">{run.subscription_id.slice(0, 8)}</td>
                <td>{run.training_start_date ?? "—"} 至 {run.training_end_date ?? "—"}</td>
                <td>{statusLabels[run.status] ?? run.status}</td>
                <td className="max-w-xs truncate text-muted-foreground">{run.error_summary ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

function ReviewsView({ query, onReview }: {
  query: QueryResult<TargetReviewItem[]>
  onReview: (decision: "approve" | "reject" | "recalculate", review: TargetReviewItem) => void
}) {
  if (query.isPending) return <PageState state="loading" title="正在加载待复核项目" description="正在核对候选目标及基准版本。" />
  if (query.error) return <ErrorState error={query.error} retry={() => void query.refetch()} />
  const pending = query.data?.filter((review) => review.status === "PENDING") ?? []
  if (!pending.length) return <PageState state="empty" title="没有待复核目标" description="大幅变化的策略目标会进入这里。" />
  return (
    <section className="mt-5 space-y-4" aria-label="待复核目标">
      {pending.map((review) => {
        const canDecide = review.version !== null && review.baseline !== null && review.candidate !== null
        return (
          <article key={review.id} className="rounded-xl border bg-card p-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h2 className="font-semibold">候选目标复核</h2>
                <p className="mt-1 text-xs text-muted-foreground">建立于 {formatDate(review.created_at, true)} · {review.reason}</p>
              </div>
              <span className="rounded-full bg-amber-100 px-2 py-1 text-xs font-medium text-amber-900">待逐项核对</span>
            </div>
            {review.candidate && review.baseline ? (
              <div className="mt-4">
                <ValuesStrip values={review.candidate.values} compare={review.baseline.values} />
                <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-3">
                  <div><dt className="text-muted-foreground">策略版本</dt><dd className="mt-1 break-all">{review.candidate.strategy_version_id ?? "—"}</dd></div>
                  <div><dt className="text-muted-foreground">数据版本</dt><dd className="mt-1">{review.candidate.data_version ?? "—"}</dd></div>
                  <div><dt className="text-muted-foreground">源码摘要</dt><dd className="mt-1 break-all font-mono">{review.candidate.source_code_hash ?? "—"}</dd></div>
                </dl>
              </div>
            ) : (
              <p className="mt-4 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">
                候选或基准版本详情不完整，不能进行复核决定。
              </p>
            )}
            <div className="mt-4 flex flex-wrap gap-2">
              <Button disabled={!canDecide || !hasAction(review.allowedActions, "APPROVE")} onClick={() => onReview("approve", review)}><CheckCircle2 />确认通过</Button>
              <Button variant="destructive" disabled={!canDecide || !hasAction(review.allowedActions, "REJECT")} onClick={() => onReview("reject", review)}><XCircle />驳回</Button>
              <Button variant="outline" disabled={review.version === null || !hasAction(review.allowedActions, "RECALCULATE")} onClick={() => onReview("recalculate", review)}><RefreshCw />重新计算</Button>
            </div>
            {review.allowedActions.length === 0 || review.version === null ? (
              <p className="mt-3 text-xs text-muted-foreground">后端未提供允许操作或复核版本，操作已安全禁用。</p>
            ) : null}
          </article>
        )
      })}
    </section>
  )
}

function OperationDialog({ operation, api, onClose, onDone }: {
  operation: Operation | null
  api: TargetManagementApi
  onClose: () => void
  onDone: (message: string) => Promise<void>
}) {
  const [reason, setReason] = useState("")
  const [confirmed, setConfirmed] = useState(false)
  const [modeConfirmed, setModeConfirmed] = useState(false)
  const [values, setValues] = useState<TargetValues | null>(null)
  const [targetDate, setTargetDate] = useState("")
  const [trainingStart, setTrainingStart] = useState("")
  const [trainingEnd, setTrainingEnd] = useState("")

  const defaults = useMemo(() => {
    if (!operation) return null
    if (operation.kind === "manual") return operation.target.values
    return null
  }, [operation])
  const currentValues = values ?? defaults
  const largeChange = operation?.kind === "manual" && currentValues
    ? valueFields.some(({ key }) => {
        const oldValue = Number(operation.target.values[key])
        return Math.abs(Number(currentValues[key]) - oldValue) / Math.max(Math.abs(oldValue), 0.01) > 0.3
      })
    : false
  const isStrategyManual = operation?.kind === "manual" && operation.target.source === "STRATEGY"

  const mutation = useMutation({
    mutationFn: async () => {
      if (!operation || !reason.trim()) throw new Error("请输入操作原因")
      if (operation.kind === "manual") {
        if (!currentValues || !targetDate) throw new Error("请填写完整目标")
        const numeric = valueFields.map(({ key }) => Number(currentValues[key]))
        if (numeric.some((value) => !Number.isFinite(value) || value <= 0) || !numeric.every((value, index) => index === 0 || numeric[index - 1] < value)) {
          throw new Error("四档价格必须为正数并严格递增")
        }
        const input: ManualTargetInput = {
          targetDate,
          values: currentValues,
          reason: reason.trim(),
          expectedVersion: operation.target.binding_version,
          largeChangeConfirmed: largeChange,
          switchToManualConfirmed: isStrategyManual,
        }
        await api.setManual(operation.target.subscription_id, input)
        return "手工目标已保存并激活"
      }
      if (operation.kind === "calculate") {
        if (!targetDate || !trainingStart || !trainingEnd || trainingStart > trainingEnd) throw new Error("请填写有效的计算日期范围")
        const input: CalculateTargetInput = {
          targetDate,
          trainingStartDate: trainingStart,
          trainingEndDate: trainingEnd,
          reason: reason.trim(),
          expectedVersion: operation.target.binding_version,
        }
        await api.calculate(operation.target.subscription_id, input)
        return "目标计算任务已提交"
      }
      if (operation.kind === "restore") {
        await api.restore(operation.target.subscription_id, {
          sourceRevisionId: operation.revision.id,
          reason: reason.trim(),
          expectedVersion: operation.target.binding_version,
          switchToManualConfirmed: true,
        })
        return "历史目标已复制为新版本并切换为手工模式"
      }
      if (operation.review.version === null) throw new Error("复核版本缺失，请重新加载")
      if (operation.decision === "approve") {
        await api.approve(operation.review.id, { comment: reason.trim(), expectedVersion: operation.review.version })
        return "候选目标已批准"
      }
      if (operation.decision === "reject") {
        await api.reject(operation.review.id, { comment: reason.trim(), expectedVersion: operation.review.version })
        return "候选目标已驳回，旧目标继续使用"
      }
      await api.recalculate(operation.review.id, reason.trim(), operation.review.version)
      return "重新计算任务已提交"
    },
    onSuccess: (message) => void onDone(message),
  })

  const close = () => {
    if (mutation.isPending) return
    setReason("")
    setConfirmed(false)
    setModeConfirmed(false)
    setValues(null)
    setTargetDate("")
    setTrainingStart("")
    setTrainingEnd("")
    mutation.reset()
    onClose()
  }
  const requiresModeConfirmation = isStrategyManual || operation?.kind === "restore"
  const canSubmit = reason.trim().length > 0
    && confirmed
    && (!requiresModeConfirmation || modeConfirmed)
    && (!largeChange || confirmed)

  return (
    <Dialog open={operation !== null} onOpenChange={(open) => { if (!open) close() }}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-3xl">
        <DialogTitle>{operationTitle(operation)}</DialogTitle>
        <DialogDescription>{operationDescription(operation)}</DialogDescription>
        {operation?.kind === "manual" && currentValues ? (
          <div className="space-y-4">
            <div className="grid gap-3 sm:grid-cols-2">
              {valueFields.map(({ key, label }) => (
                <label key={key} className="text-sm">{label}
                  <Input className="mt-1" inputMode="decimal" value={currentValues[key]} onChange={(event) => setValues({ ...currentValues, [key]: event.target.value })} />
                </label>
              ))}
              <label className="text-sm">目标日期<Input className="mt-1" type="date" value={targetDate} onChange={(event) => setTargetDate(event.target.value)} /></label>
            </div>
            <ValuesStrip values={currentValues} compare={operation.target.values} />
            {largeChange ? <p className="rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-900">至少一档变化超过 30%，请仔细核对后再确认。</p> : null}
          </div>
        ) : null}
        {operation?.kind === "calculate" ? (
          <div className="grid gap-3 sm:grid-cols-3">
            <label className="text-sm">训练开始日<Input className="mt-1" type="date" value={trainingStart} onChange={(event) => setTrainingStart(event.target.value)} /></label>
            <label className="text-sm">训练结束日<Input className="mt-1" type="date" value={trainingEnd} onChange={(event) => setTrainingEnd(event.target.value)} /></label>
            <label className="text-sm">目标日期<Input className="mt-1" type="date" value={targetDate} onChange={(event) => setTargetDate(event.target.value)} /></label>
          </div>
        ) : null}
        {operation?.kind === "restore" ? <ValuesStrip values={operation.revision.values} compare={operation.target.values} /> : null}
        {operation?.kind === "review" && operation.review.candidate && operation.review.baseline ? (
          <ValuesStrip values={operation.review.candidate.values} compare={operation.review.baseline.values} />
        ) : null}
        <label className="text-sm">操作原因
          <textarea className="mt-1 min-h-20 w-full rounded-md border bg-background px-3 py-2 text-sm" value={reason} onChange={(event) => setReason(event.target.value)} maxLength={500} />
        </label>
        {requiresModeConfirmation ? (
          <label className="flex items-start gap-2 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">
            <input className="mt-1" type="checkbox" checked={modeConfirmed} onChange={(event) => setModeConfirmed(event.target.checked)} />
            我确认订阅将切换为手工目标模式，后续策略计算不会自动覆盖本次目标。
          </label>
        ) : null}
        <label className="flex items-start gap-2 text-sm">
          <input className="mt-1" type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
          我已逐项核对上述变化，并确认执行此操作。
        </label>
        {mutation.isError ? <p role="alert" className="text-sm text-destructive">{mutation.error instanceof Error ? mutation.error.message : "操作失败，请重试。"}</p> : null}
        <DialogFooter>
          <Button variant="outline" onClick={close} disabled={mutation.isPending}>取消</Button>
          <Button onClick={() => mutation.mutate()} disabled={!canSubmit || mutation.isPending}>
            {mutation.isPending ? <Clock3 className="animate-spin" /> : null}
            {mutation.isPending ? "正在提交" : "确认执行"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function operationTitle(operation: Operation | null) {
  if (!operation) return ""
  if (operation.kind === "manual") return "编辑手工目标"
  if (operation.kind === "calculate") return "运行策略计算"
  if (operation.kind === "restore") return `恢复第 ${operation.revision.revision_no} 版目标`
  if (operation.decision === "approve") return "批准候选目标"
  if (operation.decision === "reject") return "驳回候选目标"
  return "重新计算候选目标"
}

function operationDescription(operation: Operation | null) {
  if (!operation) return ""
  if (operation.kind === "manual") return "新目标会直接激活。请对照旧值核对每一档变化。"
  if (operation.kind === "calculate") return "计算将冻结策略、参数和数据快照，并以当前绑定版本提交。"
  if (operation.kind === "restore") return "系统会复制历史价格生成新版本，不会修改原历史记录。"
  if (operation.decision === "approve") return "批准前请逐项核对新旧目标；数据变化时后端会拒绝旧版本提交。"
  if (operation.decision === "reject") return "驳回后旧目标继续服务，并保持过期状态等待后续处理。"
  return "旧候选不会被修改，系统会创建新的计算任务和候选版本。"
}
