import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  BanIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  CirclePauseIcon,
  CirclePlayIcon,
  ListRestartIcon,
  RefreshCwIcon,
  RotateCcwIcon,
  SearchIcon,
  XIcon,
} from "lucide-react"

import { jobGateway } from "@/features/jobs/gateway"
import {
  jobStatuses,
  type JobAction,
  type JobFilters,
  type JobGateway,
  type JobStatus,
  type JobSummary,
} from "@/features/jobs/types"
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

interface JobsPageProps {
  gateway?: JobGateway
}

const statusLabels: Record<JobStatus, string> = {
  PENDING_DISPATCH: "等待分发",
  QUEUED: "已排队",
  RUNNING: "运行中",
  WAITING_RETRY: "等待重试",
  PAUSING: "暂停中",
  PAUSED: "已暂停",
  CANCEL_REQUESTED: "取消中",
  SUCCEEDED: "成功",
  PARTIAL: "部分成功",
  FAILED: "失败",
  TIMED_OUT: "超时",
  LOST: "失联",
  CANCELED: "已取消",
  BLOCKED: "已阻塞",
  REJECTED: "已拒绝",
}

const actionLabels: Record<JobAction, string> = {
  cancel: "取消",
  pause: "暂停",
  resume: "继续",
  retry: "重试任务",
  "retry-failed-items": "重试失败项目",
}

const actionIcons = {
  cancel: BanIcon,
  pause: CirclePauseIcon,
  resume: CirclePlayIcon,
  retry: RotateCcwIcon,
  "retry-failed-items": ListRestartIcon,
} satisfies Record<JobAction, typeof BanIcon>

const actions: JobAction[] = [
  "pause",
  "resume",
  "cancel",
  "retry",
  "retry-failed-items",
]

function dateTime(value: string | null) {
  if (!value) return "—"
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value))
}

function statusTone(status: string) {
  if (status === "SUCCEEDED") {
    return "border-emerald-600/30 bg-emerald-600/10 text-emerald-800"
  }
  if (["FAILED", "TIMED_OUT", "LOST", "REJECTED"].includes(status)) {
    return "border-destructive/30 bg-destructive/10 text-destructive"
  }
  if (["PARTIAL", "PAUSED", "BLOCKED"].includes(status)) {
    return "border-amber-600/30 bg-amber-600/10 text-amber-800"
  }
  return "border-border bg-muted/60 text-muted-foreground"
}

function Status({ value }: { value: JobStatus | string }) {
  return (
    <span
      className={`inline-flex rounded border px-2 py-0.5 text-xs font-medium ${statusTone(value)}`}
    >
      {statusLabels[value as JobStatus] ?? value}
    </span>
  )
}

function diagnostic(error: Error) {
  return error instanceof ApiError
    ? { code: error.code, requestId: error.requestId }
    : { code: "UNKNOWN_ERROR" }
}

function progressText(job: JobSummary) {
  const completed = job.progress?.completed
  const total = job.progress?.total
  if (typeof completed === "number" && typeof total === "number") {
    return `${completed} / ${total}`
  }
  const percentage = job.progress?.percentage
  return typeof percentage === "number" ? `${percentage}%` : "—"
}

export function JobsPage({ gateway = jobGateway }: JobsPageProps) {
  const queryClient = useQueryClient()
  const [filters, setFilters] = useState<JobFilters>({
    page: 1,
    pageSize: 20,
  })
  const [draftJobType, setDraftJobType] = useState("")
  const [draftQueue, setDraftQueue] = useState("")
  const [draftStatus, setDraftStatus] = useState<JobStatus | "">("")
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null)
  const [pendingAction, setPendingAction] = useState<JobAction | null>(null)
  const [reason, setReason] = useState("")
  const [successMessage, setSuccessMessage] = useState("")

  const jobsQuery = useQuery({
    queryKey: ["jobs", "list", filters],
    queryFn: () => gateway.loadJobs(filters),
  })
  const detailsQuery = useQuery({
    queryKey: ["jobs", "details", selectedJobId],
    queryFn: () => gateway.loadDetails(selectedJobId ?? ""),
    enabled: selectedJobId !== null,
  })
  const actionMutation = useMutation({
    mutationFn: async () => {
      if (!pendingAction || !detailsQuery.data) return
      await gateway.runAction({
        jobId: detailsQuery.data.job.id,
        action: pendingAction,
        reason: reason.trim(),
        expectedVersion: detailsQuery.data.job.version,
      })
    },
    onSuccess: async () => {
      const completedAction = pendingAction
      setPendingAction(null)
      setReason("")
      setSuccessMessage(
        completedAction ? `${actionLabels[completedAction]}请求已受理。` : "",
      )
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["jobs", "list"] }),
        queryClient.invalidateQueries({
          queryKey: ["jobs", "details", selectedJobId],
        }),
      ])
    },
  })

  function applyFilters() {
    setFilters({
      page: 1,
      pageSize: filters.pageSize,
      status: draftStatus || undefined,
      jobType: draftJobType.trim() || undefined,
      queue: draftQueue.trim() || undefined,
    })
  }

  function clearFilters() {
    setDraftJobType("")
    setDraftQueue("")
    setDraftStatus("")
    setFilters({ page: 1, pageSize: filters.pageSize })
  }

  function openAction(action: JobAction) {
    if (!detailsQuery.data?.allowedActions.includes(action)) return
    setPendingAction(action)
    setReason("")
    actionMutation.reset()
  }

  const totalPages = jobsQuery.data
    ? Math.max(1, Math.ceil(
      jobsQuery.data.pagination.total / jobsQuery.data.pagination.pageSize,
    ))
    : 1

  return (
    <main className="mx-auto w-full max-w-[1500px] px-4 py-5 sm:px-6">
      <header className="mb-5 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">任务管理</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            查看任务进度、运行尝试和逐项结果
          </p>
        </div>
        <Button
          size="icon-sm"
          variant="outline"
          aria-label="刷新任务列表"
          title="刷新任务列表"
          disabled={jobsQuery.isFetching}
          onClick={() => void jobsQuery.refetch()}
        >
          <RefreshCwIcon className={jobsQuery.isFetching ? "animate-spin" : ""} />
        </Button>
      </header>

      <section className="mb-5 border-y py-4" aria-label="任务筛选">
        <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_180px_auto]">
          <Input
            aria-label="任务类型"
            placeholder="任务类型"
            value={draftJobType}
            onChange={(event) => setDraftJobType(event.target.value)}
          />
          <Input
            aria-label="队列"
            placeholder="队列"
            value={draftQueue}
            onChange={(event) => setDraftQueue(event.target.value)}
          />
          <select
            aria-label="任务状态"
            className="h-9 rounded-md border bg-background px-3 text-sm"
            value={draftStatus}
            onChange={(event) => {
              setDraftStatus(event.target.value as JobStatus | "")
            }}
          >
            <option value="">全部状态</option>
            {jobStatuses.map((status) => (
              <option key={status} value={status}>{statusLabels[status]}</option>
            ))}
          </select>
          <div className="flex gap-2">
            <Button onClick={applyFilters}>
              <SearchIcon data-icon="inline-start" />
              查询
            </Button>
            <Button
              size="icon"
              variant="outline"
              aria-label="清空筛选"
              title="清空筛选"
              onClick={clearFilters}
            >
              <XIcon />
            </Button>
          </div>
        </div>
      </section>

      {successMessage ? (
        <p role="status" className="mb-4 text-sm text-emerald-700">
          {successMessage}
        </p>
      ) : null}

      {jobsQuery.isPending ? (
        <PageState
          state="loading"
          title="正在读取任务"
          description="正在加载任务状态和进度。"
        />
      ) : jobsQuery.isError ? (
        <PageState
          state="error"
          title="任务列表暂时无法读取"
          description="请稍后重试，已有任务不会受到影响。"
          action={{
            label: "重新加载",
            onClick: () => void jobsQuery.refetch(),
          }}
          error={diagnostic(jobsQuery.error)}
        />
      ) : jobsQuery.data.items.length === 0 ? (
        <PageState
          state="empty"
          title="没有符合条件的任务"
          description="可以调整筛选条件后重新查询。"
        />
      ) : (
        <>
          <div className="overflow-x-auto border">
            <table className="w-full min-w-[900px] text-sm">
              <thead className="bg-muted/50 text-left text-xs text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 font-medium">任务</th>
                  <th className="px-3 py-2 font-medium">队列</th>
                  <th className="px-3 py-2 font-medium">状态</th>
                  <th className="px-3 py-2 font-medium">进度</th>
                  <th className="px-3 py-2 font-medium">更新时间</th>
                  <th className="px-3 py-2 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {jobsQuery.data.items.map((job) => (
                  <tr key={job.id} className="border-t">
                    <td className="px-3 py-2.5">
                      <strong className="block font-medium">{job.jobType}</strong>
                      <span className="font-mono text-xs text-muted-foreground">
                        {job.id}
                      </span>
                    </td>
                    <td className="px-3 py-2.5">{job.queue}</td>
                    <td className="px-3 py-2.5"><Status value={job.status} /></td>
                    <td className="px-3 py-2.5 tabular-nums">
                      {progressText(job)}
                    </td>
                    <td className="px-3 py-2.5">{dateTime(job.updatedAt)}</td>
                    <td className="px-3 py-2.5 text-right">
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => {
                          setSelectedJobId(job.id)
                          setSuccessMessage("")
                        }}
                      >
                        查看详情
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <footer className="mt-3 flex items-center justify-between gap-3 text-sm">
            <span className="text-muted-foreground">
              共 {jobsQuery.data.pagination.total} 项，第 {filters.page} / {totalPages} 页
            </span>
            <div className="flex gap-2">
              <Button
                size="icon-sm"
                variant="outline"
                aria-label="上一页"
                disabled={filters.page <= 1}
                onClick={() => setFilters((current) => ({
                  ...current,
                  page: current.page - 1,
                }))}
              >
                <ChevronLeftIcon />
              </Button>
              <Button
                size="icon-sm"
                variant="outline"
                aria-label="下一页"
                disabled={filters.page >= totalPages}
                onClick={() => setFilters((current) => ({
                  ...current,
                  page: current.page + 1,
                }))}
              >
                <ChevronRightIcon />
              </Button>
            </div>
          </footer>
        </>
      )}

      <Dialog
        open={selectedJobId !== null}
        onOpenChange={(open) => {
          if (!open) {
            setSelectedJobId(null)
            setPendingAction(null)
          }
        }}
      >
        <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-4xl">
          <DialogTitle>任务详情</DialogTitle>
          <DialogDescription>
            查看安全摘要、运行尝试和逐项结果。此处不提供命令执行或原始日志。
          </DialogDescription>
          {detailsQuery.isPending ? (
            <PageState
              state="loading"
              title="正在读取任务详情"
              description="正在并行读取运行尝试和逐项结果。"
            />
          ) : detailsQuery.isError ? (
            <PageState
              state="error"
              title="任务详情暂时无法读取"
              description="任务列表不受影响，可以稍后重新读取详情。"
              action={{
                label: "重新加载详情",
                onClick: () => void detailsQuery.refetch(),
              }}
              error={diagnostic(detailsQuery.error)}
            />
          ) : detailsQuery.data ? (
            <div className="space-y-5">
              <section className="grid gap-3 border-y py-4 sm:grid-cols-2 lg:grid-cols-4">
                <div>
                  <span className="text-xs text-muted-foreground">任务类型</span>
                  <strong className="block text-sm">
                    {detailsQuery.data.job.jobType}
                  </strong>
                </div>
                <div>
                  <span className="text-xs text-muted-foreground">当前状态</span>
                  <div className="mt-1">
                    <Status value={detailsQuery.data.job.status} />
                  </div>
                </div>
                <div>
                  <span className="text-xs text-muted-foreground">版本</span>
                  <strong className="block text-sm">
                    v{detailsQuery.data.job.version}
                  </strong>
                </div>
                <div>
                  <span className="text-xs text-muted-foreground">请求标识</span>
                  <strong className="block truncate text-sm">
                    {detailsQuery.data.job.requestId}
                  </strong>
                </div>
              </section>

              <section>
                <h2 className="mb-2 text-base font-semibold">任务操作</h2>
                <div className="flex flex-wrap gap-2">
                  {actions.map((action) => {
                    const Icon = actionIcons[action]
                    const isAllowed =
                      detailsQuery.data.allowedActions.includes(action)
                    return (
                      <Button
                        key={action}
                        size="sm"
                        variant={action === "cancel" ? "destructive" : "outline"}
                        disabled={!isAllowed || actionMutation.isPending}
                        title={isAllowed
                          ? actionLabels[action]
                          : "当前状态不允许此操作"}
                        onClick={() => openAction(action)}
                      >
                        <Icon data-icon="inline-start" />
                        {actionLabels[action]}
                      </Button>
                    )
                  })}
                </div>
              </section>

              <section>
                <h2 className="mb-2 text-base font-semibold">运行尝试</h2>
                {detailsQuery.data.runs.length === 0 ? (
                  <p className="text-sm text-muted-foreground">暂无运行尝试。</p>
                ) : (
                  <div className="overflow-x-auto border">
                    <table className="w-full min-w-[620px] text-sm">
                      <thead className="bg-muted/50 text-left text-xs text-muted-foreground">
                        <tr>
                          <th className="px-3 py-2 font-medium">次数</th>
                          <th className="px-3 py-2 font-medium">状态</th>
                          <th className="px-3 py-2 font-medium">Worker</th>
                          <th className="px-3 py-2 font-medium">开始</th>
                          <th className="px-3 py-2 font-medium">结束</th>
                          <th className="px-3 py-2 font-medium">错误码</th>
                        </tr>
                      </thead>
                      <tbody>
                        {detailsQuery.data.runs.map((run) => (
                          <tr key={run.id} className="border-t">
                            <td className="px-3 py-2">#{run.attemptNo}</td>
                            <td className="px-3 py-2"><Status value={run.status} /></td>
                            <td className="px-3 py-2">{run.workerId ?? "—"}</td>
                            <td className="px-3 py-2">{dateTime(run.startedAt)}</td>
                            <td className="px-3 py-2">{dateTime(run.endedAt)}</td>
                            <td className="px-3 py-2">{run.errorCode ?? "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>

              <section>
                <h2 className="mb-2 text-base font-semibold">
                  逐项结果
                  <span className="ml-2 text-xs font-normal text-muted-foreground">
                    共 {detailsQuery.data.itemPagination.total} 项
                  </span>
                </h2>
                {detailsQuery.data.items.length === 0 ? (
                  <p className="text-sm text-muted-foreground">此任务没有逐项结果。</p>
                ) : (
                  <div className="max-h-64 overflow-auto border">
                    <table className="w-full min-w-[600px] text-sm">
                      <thead className="sticky top-0 bg-muted text-left text-xs text-muted-foreground">
                        <tr>
                          <th className="px-3 py-2 font-medium">项目</th>
                          <th className="px-3 py-2 font-medium">状态</th>
                          <th className="px-3 py-2 font-medium">尝试次数</th>
                          <th className="px-3 py-2 font-medium">错误码</th>
                          <th className="px-3 py-2 font-medium">更新时间</th>
                        </tr>
                      </thead>
                      <tbody>
                        {detailsQuery.data.items.map((item) => (
                          <tr key={item.id} className="border-t">
                            <td className="px-3 py-2">{item.itemKey}</td>
                            <td className="px-3 py-2"><Status value={item.status} /></td>
                            <td className="px-3 py-2">{item.attemptCount}</td>
                            <td className="px-3 py-2">{item.errorCode ?? "—"}</td>
                            <td className="px-3 py-2">{dateTime(item.updatedAt)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>
            </div>
          ) : null}
        </DialogContent>
      </Dialog>

      <Dialog
        open={pendingAction !== null}
        onOpenChange={(open) => {
          if (!open && !actionMutation.isPending) {
            setPendingAction(null)
            setReason("")
          }
        }}
      >
        <DialogContent>
          <DialogTitle>
            {pendingAction ? `确认${actionLabels[pendingAction]}` : "确认操作"}
          </DialogTitle>
          <DialogDescription>
            系统将按当前任务版本提交请求，实际结果仍由服务器校验。
          </DialogDescription>
          <label className="space-y-2 text-sm">
            <span>操作原因</span>
            <Input
              aria-label="操作原因"
              value={reason}
              maxLength={200}
              autoFocus
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
          {actionMutation.isError ? (
            <p role="alert" className="text-sm text-destructive">
              {actionMutation.error instanceof Error
                ? actionMutation.error.message
                : "任务操作失败，请重新读取任务状态后再试。"}
            </p>
          ) : null}
          <DialogFooter>
            <Button
              variant="outline"
              disabled={actionMutation.isPending}
              onClick={() => setPendingAction(null)}
            >
              返回
            </Button>
            <Button
              disabled={!reason.trim() || actionMutation.isPending}
              onClick={() => actionMutation.mutate()}
            >
              确认执行
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </main>
  )
}
