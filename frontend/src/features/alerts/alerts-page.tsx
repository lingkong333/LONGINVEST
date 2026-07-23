import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query"
import {
  BellRing,
  Check,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Clock3,
  RefreshCw,
  RotateCcw,
  Search,
  ShieldAlert,
  X,
} from "lucide-react"
import { useEffect, useState } from "react"

import { useAuth } from "@/features/auth"
import { alertGateway } from "@/features/alerts/gateway"
import type {
  AlertAllowedAction,
  AlertGateway,
  AlertHistoryAction,
  AlertItem,
  AlertSeverity,
  AlertStatus,
} from "@/features/alerts/types"
import { ApiError } from "@/shared/api/client"
import { Alert, AlertDescription } from "@/shared/ui/alert"
import { Badge } from "@/shared/ui/badge"
import { Button } from "@/shared/ui/button"
import { Card, CardContent } from "@/shared/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/shared/ui/dialog"
import { Input } from "@/shared/ui/input"
import { PageState } from "@/shared/ui/page-state"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/select"

const PAGE_SIZE = 20
const ALL_FILTER_VALUE = "__all__"
const allActions: AlertAllowedAction[] = ["ACKNOWLEDGE", "RESOLVE", "RETRY"]

const severityLabels: Record<AlertSeverity, string> = {
  INFO: "提示",
  WARNING: "警告",
  ERROR: "错误",
  CRITICAL: "严重",
}

const statusLabels: Record<AlertStatus, string> = {
  OPEN: "待处理",
  ACKNOWLEDGED: "已确认",
  RESOLVED: "已解决",
}

const historyLabels: Record<AlertHistoryAction, string> = {
  OPENED: "告警发生",
  UPDATED: "告警更新",
  ESCALATED: "严重度升级",
  REOPENED: "再次发生",
  ACKNOWLEDGED: "确认已知",
  RESOLVED: "人工解决",
  AUTO_RESOLVED: "自动恢复",
  RETRY_REQUESTED: "提交重试",
}

const actionCopy: Record<
  AlertAllowedAction,
  { label: string; description: string; confirmation: string }
> = {
  ACKNOWLEDGE: {
    label: "确认已知",
    description: "仅记录你已看到这条告警，不代表问题已经恢复，也不会停止后续提醒。",
    confirmation: "确认已知",
  },
  RESOLVE: {
    label: "人工解决",
    description: "仅在问题已经处理完成时使用，处理说明会永久保存在告警历史中。",
    confirmation: "确认解决",
  },
  RETRY: {
    label: "提交重试",
    description: "系统只会创建后台重试任务，当前页面不会直接执行耗时操作。",
    confirmation: "创建任务",
  },
}

function formatTime(value: string | null) {
  if (!value) return "暂无"
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date)
}

function errorDetails(error: unknown, fallbackCode: string) {
  return error instanceof ApiError
    ? { code: error.code, requestId: error.requestId }
    : { code: fallbackCode }
}

function ActionIcon({ action }: { action: AlertAllowedAction }) {
  if (action === "ACKNOWLEDGE") return <Check aria-hidden="true" />
  if (action === "RESOLVE") return <CheckCircle2 aria-hidden="true" />
  return <RotateCcw aria-hidden="true" />
}

function DetailPanel({
  alertId,
  gateway,
  onClose,
}: {
  alertId: string
  gateway: AlertGateway
  onClose(): void
}) {
  const { invalidate } = useAuth()
  const queryClient = useQueryClient()
  const [pendingAction, setPendingAction] = useState<AlertAllowedAction | null>(null)
  const [reason, setReason] = useState("")
  const [successMessage, setSuccessMessage] = useState("")

  const detailQuery = useQuery({
    queryKey: ["alerts", "detail", alertId],
    queryFn: () => gateway.loadAlert(alertId),
  })
  const occurrencesQuery = useQuery({
    queryKey: ["alerts", "occurrences", alertId],
    queryFn: () => gateway.loadOccurrences(alertId),
  })
  const actionsQuery = useQuery({
    queryKey: ["alerts", "actions", alertId],
    queryFn: () => gateway.loadActions(alertId),
  })
  const operation = useMutation({
    mutationFn: async () => {
      if (!pendingAction || !detailQuery.data) {
        throw new Error("当前告警状态不可用，请刷新后重试。")
      }
      return gateway.runAction({
        alertId,
        action: pendingAction,
        expectedVersion: detailQuery.data.version,
        reason: reason.trim(),
      })
    },
    onSuccess: async (result) => {
      setSuccessMessage(
        result.jobId
          ? `重试任务已创建，任务编号：${result.jobId}`
          : `${actionCopy[pendingAction ?? "ACKNOWLEDGE"].label}已完成`,
      )
      setPendingAction(null)
      setReason("")
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["alerts", "list"] }),
        queryClient.invalidateQueries({ queryKey: ["alerts", "detail", alertId] }),
        queryClient.invalidateQueries({ queryKey: ["alerts", "occurrences", alertId] }),
        queryClient.invalidateQueries({ queryKey: ["alerts", "actions", alertId] }),
      ])
    },
  })

  useEffect(() => {
    const errors = [
      detailQuery.error,
      occurrencesQuery.error,
      actionsQuery.error,
      operation.error,
    ]
    if (errors.some((error) => error instanceof ApiError && error.status === 401)) {
      invalidate()
    }
  }, [
    actionsQuery.error,
    detailQuery.error,
    invalidate,
    occurrencesQuery.error,
    operation.error,
  ])

  const alert = detailQuery.data
  const openAction = (action: AlertAllowedAction) => {
    if (!alert?.allowedActions.includes(action) || operation.isPending) return
    operation.reset()
    setReason("")
    setSuccessMessage("")
    setPendingAction(action)
  }

  return (
    <aside
      className="fixed inset-y-0 right-0 z-30 w-full overflow-y-auto border-l bg-background p-5 shadow-xl sm:max-w-2xl"
      aria-label="告警详情"
    >
      <header className="mb-5 flex items-start justify-between gap-4 border-b pb-4">
        <div>
          <p className="text-sm text-muted-foreground">告警详情</p>
          <h2 className="mt-1 text-xl font-semibold">{alert?.title ?? "正在读取"}</h2>
        </div>
        <Button
          type="button"
          size="icon-sm"
          variant="ghost"
          aria-label="关闭告警详情"
          onClick={onClose}
        >
          <X aria-hidden="true" />
        </Button>
      </header>

      {detailQuery.isPending ? (
        <PageState
          state="loading"
          title="正在读取告警"
          description="正在获取最新状态和允许操作。"
        />
      ) : detailQuery.isError ? (
        <PageState
          state="error"
          title="告警详情读取失败"
          description="请重试，操作按钮已保持禁用。"
          error={errorDetails(detailQuery.error, "ALERT_DETAIL_UNAVAILABLE")}
          action={{ label: "重新读取", onClick: () => void detailQuery.refetch() }}
        />
      ) : alert ? (
        <>
          <section className="grid gap-3 border-b pb-5 sm:grid-cols-2" aria-label="告警状态">
            <div>
              <span className="text-xs text-muted-foreground">当前状态</span>
              <p className="mt-1 font-medium">{statusLabels[alert.status]}</p>
            </div>
            <div>
              <span className="text-xs text-muted-foreground">严重程度</span>
              <p className="mt-1 font-medium">{severityLabels[alert.severity]}</p>
            </div>
            <div>
              <span className="text-xs text-muted-foreground">告警类型</span>
              <p className="mt-1 break-all font-mono text-sm">{alert.alertType}</p>
            </div>
            <div>
              <span className="text-xs text-muted-foreground">关联对象</span>
              <p className="mt-1 break-all text-sm">{alert.objectType} / {alert.objectId}</p>
            </div>
            <div className="sm:col-span-2">
              <span className="text-xs text-muted-foreground">最新说明</span>
              <p className="mt-1 whitespace-pre-wrap text-sm">{alert.summary}</p>
            </div>
            <div>
              <span className="text-xs text-muted-foreground">首次发生</span>
              <p className="mt-1 text-sm">{formatTime(alert.firstSeenAt)}</p>
            </div>
            <div>
              <span className="text-xs text-muted-foreground">最近发生</span>
              <p className="mt-1 text-sm">{formatTime(alert.lastSeenAt)}</p>
            </div>
            {alert.resolutionReason ? (
              <div className="sm:col-span-2">
                <span className="text-xs text-muted-foreground">解决说明</span>
                <p className="mt-1 whitespace-pre-wrap text-sm">{alert.resolutionReason}</p>
              </div>
            ) : null}
          </section>

          <section className="border-b py-5" aria-label="告警操作">
            <h3 className="mb-3 text-sm font-semibold">处理操作</h3>
            <div className="flex flex-wrap gap-2">
              {allActions.map((action) => {
                const isAllowed = alert.allowedActions.includes(action)
                return (
                  <Button
                    type="button"
                    size="sm"
                    variant={action === "RESOLVE" ? "default" : "outline"}
                    key={action}
                    disabled={!isAllowed || operation.isPending}
                    title={isAllowed ? actionCopy[action].description : "当前状态不允许此操作"}
                    onClick={() => openAction(action)}
                  >
                    <ActionIcon action={action} />
                    {actionCopy[action].label}
                  </Button>
                )
              })}
            </div>
            <p className="mt-3 text-xs text-muted-foreground">
              确认已知不等于问题解决；只有问题已处理完成后才能人工解决。
            </p>
            {successMessage ? (
              <Alert className="mt-3"><AlertDescription role="status">{successMessage}</AlertDescription></Alert>
            ) : null}
          </section>

          <section className="border-b py-5" aria-label="告警详细数据">
            <h3 className="mb-3 text-sm font-semibold">详细数据</h3>
            {Object.keys(alert.details).length === 0 ? (
              <p className="text-sm text-muted-foreground">没有附加数据。</p>
            ) : (
              <pre className="max-h-52 overflow-auto whitespace-pre-wrap break-all bg-muted p-3 text-xs">
                {JSON.stringify(alert.details, null, 2)}
              </pre>
            )}
          </section>
        </>
      ) : null}

      <section className="border-b py-5" aria-label="发生记录">
        <h3 className="mb-3 text-sm font-semibold">发生记录</h3>
        {occurrencesQuery.isPending ? (
          <p className="text-sm text-muted-foreground">正在读取发生记录...</p>
        ) : occurrencesQuery.isError ? (
          <Alert variant="destructive">
            <AlertDescription className="flex items-center justify-between gap-3">
            <span>发生记录读取失败。</span>
            <Button size="sm" variant="outline" onClick={() => void occurrencesQuery.refetch()}>
              <RefreshCw data-icon="inline-start" aria-hidden="true" />重试
            </Button>
            </AlertDescription>
          </Alert>
        ) : occurrencesQuery.data?.items.length === 0 ? (
          <p className="text-sm text-muted-foreground">暂无发生记录。</p>
        ) : (
          <ol className="space-y-3">
            {occurrencesQuery.data?.items.map((item) => (
              <li className="border-l-2 pl-3 text-sm" key={item.id}>
                <div className="flex flex-wrap justify-between gap-2">
                  <strong>{severityLabels[item.severity]}</strong>
                  <time dateTime={item.occurredAt}>{formatTime(item.occurredAt)}</time>
                </div>
                <p className="mt-1">{item.summary}</p>
                <code className="mt-1 block break-all text-xs text-muted-foreground">
                  请求标识：{item.requestId}
                </code>
              </li>
            ))}
          </ol>
        )}
      </section>

      <section className="py-5" aria-label="处理历史">
        <h3 className="mb-3 text-sm font-semibold">处理历史</h3>
        {actionsQuery.isPending ? (
          <p className="text-sm text-muted-foreground">正在读取处理历史...</p>
        ) : actionsQuery.isError ? (
          <Alert variant="destructive"><AlertDescription className="flex items-center justify-between gap-3">
            <span>处理历史读取失败。</span>
            <Button size="sm" variant="outline" onClick={() => void actionsQuery.refetch()}>
              <RefreshCw data-icon="inline-start" aria-hidden="true" />重试
            </Button>
          </AlertDescription></Alert>
        ) : actionsQuery.data?.items.length === 0 ? (
          <p className="text-sm text-muted-foreground">暂无处理历史。</p>
        ) : (
          <ol className="space-y-3">
            {actionsQuery.data?.items.map((item) => (
              <li className="flex gap-3 text-sm" key={item.id}>
                <Clock3 className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
                <div>
                  <strong>{historyLabels[item.action]}</strong>
                  <time className="ml-2 text-muted-foreground" dateTime={item.createdAt}>
                    {formatTime(item.createdAt)}
                  </time>
                  <p className="mt-1">{item.reason ?? "系统自动记录"}</p>
                  {item.jobId ? <code className="text-xs">任务编号：{item.jobId}</code> : null}
                </div>
              </li>
            ))}
          </ol>
        )}
      </section>

      <Dialog
        open={pendingAction !== null}
        onOpenChange={(open) => {
          if (!open && !operation.isPending) {
            setPendingAction(null)
            setReason("")
          }
        }}
      >
        <DialogContent
          showCloseButton={false}
          onEscapeKeyDown={(event) => {
            if (operation.isPending) event.preventDefault()
          }}
          onPointerDownOutside={(event) => {
            if (operation.isPending) event.preventDefault()
          }}
        >
          <DialogTitle>
            {pendingAction ? actionCopy[pendingAction].label : "处理告警"}
          </DialogTitle>
          <DialogDescription>
            {pendingAction ? actionCopy[pendingAction].description : ""}
          </DialogDescription>
          <label className="grid gap-2 text-sm">
            <span>{pendingAction === "RESOLVE" ? "处理说明" : "操作原因"}</span>
            <Input
              aria-label={pendingAction === "RESOLVE" ? "处理说明" : "操作原因"}
              autoFocus
              maxLength={500}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder={pendingAction === "RESOLVE" ? "请说明问题如何解决" : "请填写操作原因"}
            />
          </label>
          {operation.isError ? (
            <p className="text-sm text-destructive" role="alert">
              {operation.error instanceof Error
                ? operation.error.message
                : "操作失败，请刷新告警状态后重试。"}
            </p>
          ) : null}
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="outline"
              disabled={operation.isPending}
              onClick={() => {
                setPendingAction(null)
                setReason("")
              }}
            >
              返回
            </Button>
            <Button
              type="button"
              disabled={!reason.trim() || operation.isPending}
              onClick={() => operation.mutate()}
            >
              {operation.isPending
                ? "正在提交"
                : actionCopy[pendingAction ?? "ACKNOWLEDGE"].confirmation}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </aside>
  )
}

export function AlertsPage({
  gateway = alertGateway,
}: {
  gateway?: AlertGateway
}) {
  const { invalidate } = useAuth()
  const [page, setPage] = useState(1)
  const [status, setStatus] = useState<AlertStatus | "">("")
  const [severity, setSeverity] = useState<AlertSeverity | "">("")
  const [alertTypeInput, setAlertTypeInput] = useState("")
  const [alertType, setAlertType] = useState("")
  const [selectedAlertId, setSelectedAlertId] = useState<string | null>(null)

  const listQuery = useQuery({
    queryKey: ["alerts", "list", page, status, severity, alertType],
    queryFn: () => gateway.loadAlerts({
      page,
      pageSize: PAGE_SIZE,
      status: status || undefined,
      severity: severity || undefined,
      alertType: alertType || undefined,
    }),
    placeholderData: keepPreviousData,
  })

  useEffect(() => {
    if (listQuery.error instanceof ApiError && listQuery.error.status === 401) {
      invalidate()
    }
  }, [invalidate, listQuery.error])

  const applySearch = () => {
    setPage(1)
    setAlertType(alertTypeInput.trim())
  }
  const updateStatus = (value: AlertStatus | "") => {
    setPage(1)
    setStatus(value)
  }
  const updateSeverity = (value: AlertSeverity | "") => {
    setPage(1)
    setSeverity(value)
  }
  const totalPages = Math.max(1, Math.ceil((listQuery.data?.total ?? 0) / PAGE_SIZE))

  return (
    <main className="mx-auto w-full max-w-7xl px-4 py-5 sm:px-6">
      <header className="mb-5 flex flex-wrap items-center justify-between gap-4 border-b pb-4">
        <div className="flex items-center gap-3">
          <span className="grid size-10 place-items-center bg-destructive/10 text-destructive">
            <ShieldAlert aria-hidden="true" />
          </span>
          <div>
            <p className="text-sm text-muted-foreground">运行保障</p>
            <h1 className="text-2xl font-semibold">系统告警</h1>
          </div>
        </div>
        <Button
          type="button"
          size="icon-sm"
          variant="ghost"
          aria-label="刷新告警列表"
          disabled={listQuery.isFetching}
          onClick={() => void listQuery.refetch()}
        >
          <RefreshCw data-icon="icon" className={listQuery.isFetching ? "animate-spin" : undefined} />
        </Button>
      </header>

      <section className="mb-5 flex flex-wrap items-end gap-3" aria-label="告警筛选">
        <label className="grid gap-1 text-sm">
          <span>状态</span>
          <Select
            value={status || ALL_FILTER_VALUE}
            onValueChange={(value) => updateStatus(
              value === ALL_FILTER_VALUE ? "" : value as AlertStatus,
            )}
          >
            <SelectTrigger aria-label="按状态筛选"><SelectValue /></SelectTrigger>
            <SelectContent><SelectGroup>
              <SelectItem value={ALL_FILTER_VALUE}>全部状态</SelectItem>
              <SelectItem value="OPEN">待处理</SelectItem>
              <SelectItem value="ACKNOWLEDGED">已确认</SelectItem>
              <SelectItem value="RESOLVED">已解决</SelectItem>
            </SelectGroup></SelectContent>
          </Select>
        </label>
        <label className="grid gap-1 text-sm">
          <span>严重程度</span>
          <Select
            value={severity || ALL_FILTER_VALUE}
            onValueChange={(value) => updateSeverity(
              value === ALL_FILTER_VALUE ? "" : value as AlertSeverity,
            )}
          >
            <SelectTrigger aria-label="按严重程度筛选"><SelectValue /></SelectTrigger>
            <SelectContent><SelectGroup>
              <SelectItem value={ALL_FILTER_VALUE}>全部级别</SelectItem>
              <SelectItem value="INFO">提示</SelectItem>
              <SelectItem value="WARNING">警告</SelectItem>
              <SelectItem value="ERROR">错误</SelectItem>
              <SelectItem value="CRITICAL">严重</SelectItem>
            </SelectGroup></SelectContent>
          </Select>
        </label>
        <form
          className="flex min-w-64 flex-1 gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            applySearch()
          }}
        >
          <label className="relative flex-1">
            <Search className="absolute left-3 top-2.5 size-4 text-muted-foreground" />
            <span className="sr-only">按告警类型筛选</span>
            <Input
              className="pl-9"
              aria-label="按告警类型筛选"
              value={alertTypeInput}
              onChange={(event) => setAlertTypeInput(event.target.value)}
              placeholder="输入告警类型"
            />
          </label>
          <Button type="submit" variant="outline">筛选</Button>
        </form>
      </section>

      {listQuery.isPending ? (
        <PageState
          state="loading"
          title="正在读取系统告警"
          description="正在获取最新告警及处理状态。"
        />
      ) : listQuery.isError ? (
        <PageState
          state="error"
          title="系统告警读取失败"
          description="请检查连接后重试，当前不会执行任何操作。"
          error={errorDetails(listQuery.error, "ALERT_LIST_UNAVAILABLE")}
          action={{ label: "重新读取", onClick: () => void listQuery.refetch() }}
        />
      ) : listQuery.data.items.length === 0 ? (
        <PageState
          state="empty"
          title="没有符合条件的告警"
          description="当前筛选范围内没有系统告警。"
        />
      ) : (
        <>
          <section className="grid gap-3" aria-label="系统告警列表">
            {listQuery.data.items.map((alert: AlertItem) => (
              <Card
                key={alert.id}
              >
                <CardContent className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
                <div className="min-w-0">
                  <div className="mb-2 flex flex-wrap items-center gap-2">
                    <Badge variant={alert.severity === "CRITICAL" || alert.severity === "ERROR" ? "destructive" : "outline"}>
                      {severityLabels[alert.severity]}
                    </Badge>
                    <Badge variant="secondary">
                      {statusLabels[alert.status]}
                    </Badge>
                    <code className="break-all text-xs text-muted-foreground">
                      {alert.alertType}
                    </code>
                  </div>
                  <h2 className="font-semibold">{alert.title}</h2>
                  <p className="mt-1 line-clamp-2 text-sm text-muted-foreground">
                    {alert.summary}
                  </p>
                  <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
                    <span>累计 {alert.occurrenceCount} 次</span>
                    <time dateTime={alert.lastSeenAt}>最近：{formatTime(alert.lastSeenAt)}</time>
                    <span>{alert.objectType} / {alert.objectId}</span>
                  </div>
                </div>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => setSelectedAlertId(alert.id)}
                >
                  <BellRing data-icon="inline-start" aria-hidden="true" />
                  查看详情
                </Button>
                </CardContent>
              </Card>
            ))}
          </section>
          <nav
            className="mt-4 flex items-center justify-between gap-4"
            aria-label="告警列表分页"
          >
            <span className="text-sm text-muted-foreground">
              共 {listQuery.data.total} 条，第 {listQuery.data.page} / {totalPages} 页
            </span>
            <div className="flex gap-2">
              <Button
                type="button"
                size="icon-sm"
                variant="outline"
                aria-label="上一页"
                disabled={page <= 1 || listQuery.isFetching}
                onClick={() => setPage((current) => Math.max(1, current - 1))}
              >
                <ChevronLeft data-icon="icon" aria-hidden="true" />
              </Button>
              <Button
                type="button"
                size="icon-sm"
                variant="outline"
                aria-label="下一页"
                disabled={page >= totalPages || listQuery.isFetching}
                onClick={() => setPage((current) => current + 1)}
              >
                <ChevronRight data-icon="icon" aria-hidden="true" />
              </Button>
            </div>
          </nav>
        </>
      )}

      {selectedAlertId ? (
        <DetailPanel
          alertId={selectedAlertId}
          gateway={gateway}
          onClose={() => setSelectedAlertId(null)}
        />
      ) : null}
    </main>
  )
}
