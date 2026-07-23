import { useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import {
  ChevronLeftIcon,
  ChevronRightIcon,
  EyeIcon,
  FilterIcon,
  RefreshCwIcon,
  ShieldCheckIcon,
  XIcon,
} from "lucide-react"
import { useSearchParams } from "react-router-dom"

import { auditGateway } from "@/features/audit/gateway"
import type {
  AuditEvent,
  AuditFilters,
  AuditGateway,
} from "@/features/audit/types"
import { ApiError } from "@/shared/api/client"
import { Button } from "@/shared/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/shared/ui/dialog"
import { Input } from "@/shared/ui/input"
import { PageState } from "@/shared/ui/page-state"

interface AuditPageProps {
  gateway?: AuditGateway
}

interface FilterDraft {
  startAt: string
  endAt: string
  actorUserId: string
  actionCode: string
  objectType: string
  objectId: string
  result: string
  riskLevel: string
  requestId: string
}

const emptyDraft: FilterDraft = {
  startAt: "",
  endAt: "",
  actorUserId: "",
  actionCode: "",
  objectType: "",
  objectId: "",
  result: "",
  riskLevel: "",
  requestId: "",
}

function positiveInteger(value: string | null, fallback: number) {
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback
}

function isoDateTime(value: string | undefined) {
  if (!value) return undefined
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? undefined : date.toISOString()
}

function filtersFrom(search: URLSearchParams): AuditFilters {
  return {
    page: positiveInteger(search.get("page"), 1),
    pageSize: Math.min(200, positiveInteger(search.get("page_size"), 20)),
    startAt: isoDateTime(search.get("start_at") ?? undefined),
    endAt: isoDateTime(search.get("end_at") ?? undefined),
    actorUserId: search.get("actor_user_id") || undefined,
    actionCode: search.get("action_code") || undefined,
    objectType: search.get("object_type") || undefined,
    objectId: search.get("object_id") || undefined,
    result: search.get("result") || undefined,
    riskLevel: search.get("risk_level") || undefined,
    requestId: search.get("request_id") || undefined,
  }
}

function draftFrom(search: URLSearchParams): FilterDraft {
  return {
    startAt: search.get("start_at") ?? "",
    endAt: search.get("end_at") ?? "",
    actorUserId: search.get("actor_user_id") ?? "",
    actionCode: search.get("action_code") ?? "",
    objectType: search.get("object_type") ?? "",
    objectId: search.get("object_id") ?? "",
    result: search.get("result") ?? "",
    riskLevel: search.get("risk_level") ?? "",
    requestId: search.get("request_id") ?? "",
  }
}

function dateTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value))
}

function valueText(value: unknown) {
  if (value === null || value === undefined) return "—"
  if (typeof value === "string") return value
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value)
  }
  return JSON.stringify(value)
}

function errorDetails(error: Error) {
  return error instanceof ApiError
    ? { code: error.code, requestId: error.requestId }
    : { code: "UNKNOWN_ERROR" }
}

function SummaryChanges({ event }: { event: AuditEvent }) {
  const keys = Array.from(new Set([
    ...Object.keys(event.beforeSummary ?? {}),
    ...Object.keys(event.afterSummary ?? {}),
  ])).sort()

  if (keys.length === 0) {
    return <p className="text-sm text-muted-foreground">本次记录没有前后变更摘要。</p>
  }

  return (
    <div className="overflow-x-auto border">
      <table className="w-full min-w-[620px] text-sm">
        <thead className="bg-muted/50 text-left text-xs text-muted-foreground">
          <tr>
            <th className="px-3 py-2 font-medium">字段</th>
            <th className="px-3 py-2 font-medium">变更前</th>
            <th className="px-3 py-2 font-medium">变更后</th>
          </tr>
        </thead>
        <tbody>
          {keys.map((key) => (
            <tr key={key} className="border-t align-top">
              <th className="px-3 py-2 text-left font-medium">{key}</th>
              <td className="max-w-[280px] break-words px-3 py-2">
                {valueText(event.beforeSummary?.[key])}
              </td>
              <td className="max-w-[280px] break-words px-3 py-2">
                {valueText(event.afterSummary?.[key])}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function AuditPage({ gateway = auditGateway }: AuditPageProps) {
  const [searchParams, setSearchParams] = useSearchParams()
  const filters = useMemo(() => filtersFrom(searchParams), [searchParams])
  const [draft, setDraft] = useState(() => draftFrom(searchParams))
  const [selectedEvent, setSelectedEvent] = useState<AuditEvent | null>(null)

  const eventsQuery = useQuery({
    queryKey: ["audit-events", filters],
    queryFn: () => gateway.loadEvents(filters),
  })

  function updateDraft(field: keyof FilterDraft, value: string) {
    setDraft((current) => ({ ...current, [field]: value }))
  }

  function applyFilters() {
    const next = new URLSearchParams()
    next.set("page", "1")
    next.set("page_size", String(filters.pageSize))
    for (const [field, parameter] of [
      ["startAt", "start_at"],
      ["endAt", "end_at"],
      ["actorUserId", "actor_user_id"],
      ["actionCode", "action_code"],
      ["objectType", "object_type"],
      ["objectId", "object_id"],
      ["result", "result"],
      ["riskLevel", "risk_level"],
      ["requestId", "request_id"],
    ] as const) {
      const value = draft[field].trim()
      if (value) next.set(parameter, value)
    }
    setSearchParams(next)
  }

  function clearFilters() {
    setDraft(emptyDraft)
    setSearchParams({ page: "1", page_size: String(filters.pageSize) })
  }

  function changePage(page: number) {
    const next = new URLSearchParams(searchParams)
    next.set("page", String(page))
    setSearchParams(next)
  }

  if (eventsQuery.isPending) {
    return (
      <PageState
        state="loading"
        title="正在读取审计记录"
        description="正在加载最近的安全审计事件。"
      />
    )
  }
  if (eventsQuery.isError) {
    return (
      <PageState
        state="error"
        title="审计记录暂时无法读取"
        description="请稍后重试，已有审计记录不会受到影响。"
        action={{
          label: "重新加载",
          onClick: () => void eventsQuery.refetch(),
        }}
        error={errorDetails(eventsQuery.error)}
      />
    )
  }
  if (eventsQuery.data.allowedActions.length > 0) {
    return (
      <PageState
        state="error"
        title="审计权限响应异常"
        description="审计页面只允许查看，服务器不应返回任何修改操作。"
        error={{ code: "AUDIT_ACTIONS_NOT_ALLOWED" }}
      />
    )
  }

  const totalPages = Math.max(1, Math.ceil(
    eventsQuery.data.pagination.total / eventsQuery.data.pagination.pageSize,
  ))

  return (
    <main className="mx-auto w-full max-w-[1500px] px-4 py-5 sm:px-6">
      <header className="mb-5 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">审计记录</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            查看操作结果、请求线索和安全处理后的变更摘要
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span className="inline-flex items-center gap-1.5 text-sm text-muted-foreground">
            <ShieldCheckIcon className="size-4 text-emerald-700" />
            只读 · 无可用操作
          </span>
          <Button
            size="icon-sm"
            variant="outline"
            aria-label="刷新审计记录"
            title="刷新审计记录"
            disabled={eventsQuery.isFetching}
            onClick={() => void eventsQuery.refetch()}
          >
            <RefreshCwIcon className={eventsQuery.isFetching ? "animate-spin" : ""} />
          </Button>
        </div>
      </header>

      <section className="mb-5 border-y py-4" aria-label="审计筛选">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
          <label className="grid gap-1.5 text-xs text-muted-foreground">
            开始时间
            <Input
              type="datetime-local"
              value={draft.startAt}
              onChange={(event) => updateDraft("startAt", event.target.value)}
            />
          </label>
          <label className="grid gap-1.5 text-xs text-muted-foreground">
            结束时间
            <Input
              type="datetime-local"
              value={draft.endAt}
              onChange={(event) => updateDraft("endAt", event.target.value)}
            />
          </label>
          <label className="grid gap-1.5 text-xs text-muted-foreground">
            用户标识
            <Input
              value={draft.actorUserId}
              onChange={(event) => updateDraft("actorUserId", event.target.value)}
            />
          </label>
          <label className="grid gap-1.5 text-xs text-muted-foreground">
            操作代码
            <Input
              value={draft.actionCode}
              onChange={(event) => updateDraft("actionCode", event.target.value)}
            />
          </label>
          <label className="grid gap-1.5 text-xs text-muted-foreground">
            对象类型
            <Input
              value={draft.objectType}
              onChange={(event) => updateDraft("objectType", event.target.value)}
            />
          </label>
          <label className="grid gap-1.5 text-xs text-muted-foreground">
            对象标识
            <Input
              value={draft.objectId}
              onChange={(event) => updateDraft("objectId", event.target.value)}
            />
          </label>
          <label className="grid gap-1.5 text-xs text-muted-foreground">
            操作结果
            <Input
              value={draft.result}
              onChange={(event) => updateDraft("result", event.target.value)}
            />
          </label>
          <label className="grid gap-1.5 text-xs text-muted-foreground">
            风险等级
            <Input
              value={draft.riskLevel}
              onChange={(event) => updateDraft("riskLevel", event.target.value)}
            />
          </label>
          <label className="grid gap-1.5 text-xs text-muted-foreground">
            请求标识
            <Input
              value={draft.requestId}
              onChange={(event) => updateDraft("requestId", event.target.value)}
            />
          </label>
          <div className="flex items-end gap-2">
            <Button onClick={applyFilters}>
              <FilterIcon data-icon="inline-start" />
              应用筛选
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

      {eventsQuery.data.items.length === 0 ? (
        <PageState
          state="empty"
          title="没有符合条件的审计记录"
          description="可以调整时间范围或筛选条件后重新查询。"
        />
      ) : (
        <>
          <div className="overflow-x-auto border">
            <table className="w-full min-w-[980px] text-sm">
              <thead className="bg-muted/50 text-left text-xs text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 font-medium">发生时间</th>
                  <th className="px-3 py-2 font-medium">操作</th>
                  <th className="px-3 py-2 font-medium">业务对象</th>
                  <th className="px-3 py-2 font-medium">用户</th>
                  <th className="px-3 py-2 font-medium">结果</th>
                  <th className="px-3 py-2 font-medium">风险</th>
                  <th className="px-3 py-2 text-right font-medium">详情</th>
                </tr>
              </thead>
              <tbody>
                {eventsQuery.data.items.map((event) => (
                  <tr key={event.id} className="border-t">
                    <td className="px-3 py-2.5">{dateTime(event.occurredAt)}</td>
                    <td className="px-3 py-2.5 font-medium">{event.actionCode}</td>
                    <td className="px-3 py-2.5">
                      {event.objectType}
                      <span className="ml-2 text-xs text-muted-foreground">
                        {event.objectId}
                      </span>
                    </td>
                    <td className="px-3 py-2.5">{event.actorUserId ?? "系统"}</td>
                    <td className="px-3 py-2.5">{event.result}</td>
                    <td className="px-3 py-2.5">{event.riskLevel}</td>
                    <td className="px-3 py-2.5 text-right">
                      <Button
                        size="icon-sm"
                        variant="outline"
                        aria-label={`查看 ${event.actionCode} 审计详情`}
                        title="查看安全详情"
                        onClick={() => setSelectedEvent(event)}
                      >
                        <EyeIcon />
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <footer className="mt-3 flex items-center justify-between gap-3 text-sm">
            <span className="text-muted-foreground">
              共 {eventsQuery.data.pagination.total} 条，第 {filters.page} / {totalPages} 页
            </span>
            <div className="flex gap-2">
              <Button
                size="icon-sm"
                variant="outline"
                aria-label="上一页"
                disabled={filters.page <= 1}
                onClick={() => changePage(filters.page - 1)}
              >
                <ChevronLeftIcon />
              </Button>
              <Button
                size="icon-sm"
                variant="outline"
                aria-label="下一页"
                disabled={filters.page >= totalPages}
                onClick={() => changePage(filters.page + 1)}
              >
                <ChevronRightIcon />
              </Button>
            </div>
          </footer>
        </>
      )}

      <Dialog
        open={selectedEvent !== null}
        onOpenChange={(open) => {
          if (!open) setSelectedEvent(null)
        }}
      >
        <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-4xl">
          <DialogTitle>审计详情</DialogTitle>
          <DialogDescription>
            仅展示服务器提供的安全字段和变更摘要。
          </DialogDescription>
          {selectedEvent ? (
            <div className="space-y-5">
              <dl className="grid gap-3 border-y py-4 text-sm sm:grid-cols-2 lg:grid-cols-3">
                <div><dt className="text-xs text-muted-foreground">操作代码</dt><dd>{selectedEvent.actionCode}</dd></div>
                <div><dt className="text-xs text-muted-foreground">操作结果</dt><dd>{selectedEvent.result}</dd></div>
                <div><dt className="text-xs text-muted-foreground">风险等级</dt><dd>{selectedEvent.riskLevel}</dd></div>
                <div><dt className="text-xs text-muted-foreground">用户标识</dt><dd>{selectedEvent.actorUserId ?? "系统"}</dd></div>
                <div><dt className="text-xs text-muted-foreground">Session</dt><dd className="break-all">{selectedEvent.sessionId ?? "—"}</dd></div>
                <div><dt className="text-xs text-muted-foreground">可信 IP</dt><dd>{selectedEvent.trustedIp ?? "—"}</dd></div>
                <div><dt className="text-xs text-muted-foreground">请求标识</dt><dd className="break-all">{selectedEvent.requestId}</dd></div>
                <div><dt className="text-xs text-muted-foreground">幂等标识</dt><dd className="break-all">{selectedEvent.idempotencyKey}</dd></div>
                <div><dt className="text-xs text-muted-foreground">发生时间</dt><dd>{dateTime(selectedEvent.occurredAt)}</dd></div>
              </dl>
              <section>
                <h2 className="mb-2 text-base font-semibold">操作原因</h2>
                <p className="text-sm">{selectedEvent.reason ?? "未填写原因"}</p>
              </section>
              <section>
                <h2 className="mb-2 text-base font-semibold">前后变更</h2>
                <SummaryChanges event={selectedEvent} />
              </section>
            </div>
          ) : null}
        </DialogContent>
      </Dialog>
    </main>
  )
}
