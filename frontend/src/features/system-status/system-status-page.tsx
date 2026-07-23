import { useQuery } from "@tanstack/react-query"
import {
  Activity,
  CalendarClock,
  Cpu,
  Database,
  RefreshCw,
} from "lucide-react"
import type { ReactNode } from "react"

import { systemStatusGateway } from "@/features/system-status/gateway"
import type {
  HealthStatus,
  SystemStatusGateway,
} from "@/features/system-status/types"
import { ApiError } from "@/shared/api/client"
import { Button } from "@/shared/ui/button"

const healthLabels: Record<HealthStatus, string> = {
  HEALTHY: "健康",
  DEGRADED: "降级",
  UNAVAILABLE: "不可用",
  UNKNOWN: "未知",
}

const occurrenceLabels: Record<string, string> = {
  REALTIME_QUOTE: "实时行情",
  DAILY_MARKET_DATA: "日线更新",
  UNRESOLVED_DATA_RETRY: "缺失数据重试",
  MAINTENANCE: "系统维护",
  CREATED: "已创建",
  DISPATCH_PENDING: "等待分发",
  DISPATCHED: "已分发",
  MISSED: "已错过",
  CANCELED: "已取消",
}

function formatTime(value: string | null) {
  if (!value) return "暂无"
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    dateStyle: "short",
    timeStyle: "medium",
  }).format(new Date(value))
}

function statusText(status: HealthStatus) {
  return healthLabels[status]
}

function errorDiagnostic(error: unknown, fallback: string) {
  return error instanceof ApiError
    ? `${error.message}（${error.code}${error.requestId ? ` · ${error.requestId}` : ""}）`
    : `读取失败（${fallback}）`
}

export function SystemStatusPage({
  gateway = systemStatusGateway,
}: {
  gateway?: SystemStatusGateway
}) {
  return (
    <main className="mx-auto flex w-full max-w-[1500px] flex-col gap-6 px-4 py-6 lg:px-8">
      <header className="border-b pb-5">
        <h1 className="text-3xl font-semibold">运行状态</h1>
        <p className="mt-2 text-sm text-muted-foreground">查看系统依赖、后台处理和自动调度的当前情况。</p>
      </header>
      <OverallRegion gateway={gateway} />
      <ComponentsRegion gateway={gateway} />
      <RuntimeRegion gateway={gateway} />
      <SchedulingRegion gateway={gateway} />
      <OccurrencesRegion gateway={gateway} />
    </main>
  )
}

function OverallRegion({ gateway }: { gateway: SystemStatusGateway }) {
  const query = useQuery({
    queryKey: ["system-status", "overall"],
    queryFn: gateway.loadOverall,
  })
  return (
    <StatusRegion
      title="总体健康"
      icon={<Activity />}
      query={query}
      refreshLabel="刷新总体健康"
      empty={false}
      errorCode="SYSTEM_HEALTH_FAILED"
    >
      {query.data ? (
        <div className="grid gap-4 sm:grid-cols-3">
          <Summary label="当前状态" value={statusText(query.data.status)} />
          <Summary label="受检组件" value={`${query.data.componentCount} 个`} />
          <Summary label="异常组件" value={`${query.data.unhealthyCount} 个`} />
          <p className="text-xs text-muted-foreground sm:col-span-3">数据时间：{formatTime(query.data.updatedAt)}</p>
        </div>
      ) : null}
    </StatusRegion>
  )
}

function ComponentsRegion({ gateway }: { gateway: SystemStatusGateway }) {
  const query = useQuery({
    queryKey: ["system-status", "components"],
    queryFn: gateway.loadComponents,
  })
  return (
    <StatusRegion
      title="系统组件"
      icon={<Database />}
      query={query}
      refreshLabel="刷新系统组件"
      empty={query.data?.length === 0}
      emptyText="暂无组件状态"
      errorCode="SYSTEM_COMPONENTS_FAILED"
    >
      {query.data && query.data.length > 0 ? (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {query.data.map((component) => (
            <article key={`${component.category}-${component.name}`} className="rounded-md border p-4">
              <div className="flex items-start justify-between gap-3">
                <div><h3 className="font-medium">{component.name}</h3><p className="mt-1 text-xs text-muted-foreground">{component.category} · {component.source}</p></div>
                <strong className="text-sm">{statusText(component.status)}</strong>
              </div>
              {component.message ? <p className="mt-3 text-sm">{component.message}</p> : null}
              {component.details.length > 0 ? <dl className="mt-3 grid gap-1 text-xs text-muted-foreground">{component.details.map((detail) => <div key={detail.key} className="flex justify-between gap-3"><dt>{detail.key}</dt><dd>{detail.value}{detail.unit ? ` ${detail.unit}` : ""}</dd></div>)}</dl> : null}
              <p className="mt-3 text-xs text-muted-foreground">更新：{formatTime(component.updatedAt)}</p>
            </article>
          ))}
        </div>
      ) : null}
    </StatusRegion>
  )
}

function RuntimeRegion({ gateway }: { gateway: SystemStatusGateway }) {
  const query = useQuery({
    queryKey: ["system-status", "runtime"],
    queryFn: gateway.loadRuntime,
  })
  const isEmpty = query.data
    ? query.data.workers.length === 0 && query.data.queues.length === 0
    : false
  return (
    <StatusRegion
      title="Worker 与队列"
      icon={<Cpu />}
      query={query}
      refreshLabel="刷新 Worker 与队列"
      empty={isEmpty}
      emptyText="暂无 Worker 和队列状态"
      errorCode="SYSTEM_RUNTIME_FAILED"
    >
      {query.data && !isEmpty ? (
        <div className="grid gap-5 xl:grid-cols-2">
          <div>
            <h3 className="mb-3 text-sm font-semibold">队列</h3>
            {query.data.queues.length === 0 ? <p className="text-sm text-muted-foreground">暂无队列</p> : (
              <div className="overflow-x-auto"><table className="w-full min-w-[480px] text-left text-sm"><thead className="border-b text-muted-foreground"><tr><th className="py-2 font-medium">名称</th><th className="py-2 font-medium">状态</th><th className="py-2 font-medium">等待任务</th><th className="py-2 font-medium">活动 Worker</th></tr></thead><tbody className="divide-y">{query.data.queues.map((queue) => <tr key={queue.name}><td className="py-3">{queue.name}</td><td className="py-3">{statusText(queue.status)}</td><td className="py-3">{queue.depth}</td><td className="py-3">{queue.activeWorkers}</td></tr>)}</tbody></table></div>
            )}
          </div>
          <div>
            <h3 className="mb-3 text-sm font-semibold">Worker</h3>
            {query.data.workers.length === 0 ? <p className="text-sm text-muted-foreground">暂无 Worker</p> : (
              <div className="overflow-x-auto"><table className="w-full min-w-[520px] text-left text-sm"><thead className="border-b text-muted-foreground"><tr><th className="py-2 font-medium">标识</th><th className="py-2 font-medium">队列</th><th className="py-2 font-medium">状态</th><th className="py-2 font-medium">心跳</th></tr></thead><tbody className="divide-y">{query.data.workers.map((worker) => <tr key={worker.workerId}><td className="py-3 font-mono text-xs">{worker.workerId}</td><td className="py-3">{worker.queue}</td><td className="py-3">{worker.status}</td><td className="py-3">{formatTime(worker.heartbeatAt)}</td></tr>)}</tbody></table></div>
            )}
          </div>
        </div>
      ) : null}
    </StatusRegion>
  )
}

function SchedulingRegion({ gateway }: { gateway: SystemStatusGateway }) {
  const query = useQuery({
    queryKey: ["system-status", "scheduling"],
    queryFn: gateway.loadScheduling,
  })
  return (
    <StatusRegion
      title="调度器与系统时钟"
      icon={<CalendarClock />}
      query={query}
      refreshLabel="刷新调度器与系统时钟"
      empty={false}
      errorCode="SYSTEM_SCHEDULING_FAILED"
    >
      {query.data ? (
        <div className="grid gap-4 lg:grid-cols-2">
          <article className="rounded-md border p-4">
            <div className="flex justify-between gap-3"><h3 className="font-medium">自动调度</h3><strong>{statusText(query.data.scheduler.status)}</strong></div>
            <dl className="mt-4 grid gap-2 text-sm"><Row label="扫描间隔" value={`${query.data.scheduler.scanIntervalSeconds} 秒`} /><Row label="最近扫描" value={formatTime(query.data.scheduler.lastScanAt)} /><Row label="自动调度" value={query.data.scheduler.automaticSchedulingPaused ? "已暂停" : "运行中"} /></dl>
            {query.data.scheduler.pauseReason ? <p className="mt-3 text-sm text-destructive">{query.data.scheduler.pauseReason}</p> : null}
          </article>
          <article className="rounded-md border p-4">
            <div className="flex justify-between gap-3"><h3 className="font-medium">系统时钟</h3><strong>{statusText(query.data.clock.status)}</strong></div>
            <dl className="mt-4 grid gap-2 text-sm"><Row label="应用时间" value={formatTime(query.data.clock.applicationTime)} /><Row label="数据库时间" value={formatTime(query.data.clock.databaseTime)} /><Row label="最大偏差" value={query.data.clock.maxSkewSeconds === null ? "暂无" : `${query.data.clock.maxSkewSeconds} 秒`} /></dl>
          </article>
        </div>
      ) : null}
    </StatusRegion>
  )
}

function OccurrencesRegion({ gateway }: { gateway: SystemStatusGateway }) {
  const query = useQuery({
    queryKey: ["system-status", "occurrences"],
    queryFn: gateway.loadOccurrences,
  })
  return (
    <StatusRegion
      title="近期调度记录"
      icon={<CalendarClock />}
      query={query}
      refreshLabel="刷新近期调度记录"
      empty={query.data?.items.length === 0}
      emptyText="暂无近期调度记录"
      errorCode="SCHEDULE_OCCURRENCES_FAILED"
    >
      {query.data && query.data.items.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[820px] text-left text-sm">
            <thead className="border-b text-muted-foreground"><tr><th className="py-2 font-medium">计划时间</th><th className="py-2 font-medium">类型</th><th className="py-2 font-medium">定义</th><th className="py-2 font-medium">状态</th><th className="py-2 font-medium">说明</th></tr></thead>
            <tbody className="divide-y">{query.data.items.map((item) => <tr key={item.occurrenceId}><td className="py-3">{formatTime(item.scheduledAt)}</td><td className="py-3">{occurrenceLabels[item.occurrenceType] ?? item.occurrenceType}</td><td className="py-3">{item.definitionId}</td><td className="py-3">{occurrenceLabels[item.status] ?? item.status}</td><td className="py-3">{item.missedReason ?? "暂无"}</td></tr>)}</tbody>
          </table>
          <p className="mt-3 text-xs text-muted-foreground">共 {query.data.total} 条记录</p>
        </div>
      ) : null}
    </StatusRegion>
  )
}

interface QueryState {
  isPending: boolean
  isError: boolean
  error: unknown
  isFetching: boolean
  refetch: () => Promise<unknown>
}

function StatusRegion({
  title,
  icon,
  query,
  refreshLabel,
  empty,
  emptyText = "暂无数据",
  errorCode,
  children,
}: {
  title: string
  icon: ReactNode
  query: QueryState
  refreshLabel: string
  empty: boolean
  emptyText?: string
  errorCode: string
  children: ReactNode
}) {
  return (
    <section className="rounded-lg border bg-card p-5" aria-label={title}>
      <header className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-3"><span className="text-muted-foreground">{icon}</span><h2 className="font-semibold">{title}</h2></div>
        <Button size="icon-sm" variant="outline" aria-label={refreshLabel} title={refreshLabel} disabled={query.isFetching} onClick={() => void query.refetch()}><RefreshCw className={query.isFetching ? "animate-spin" : undefined} /></Button>
      </header>
      {query.isPending ? <p className="mt-5 text-sm text-muted-foreground" role="status">正在读取{title}…</p> : query.isError ? <div className="mt-5" role="alert"><p className="text-sm">{errorDiagnostic(query.error, errorCode)}</p><Button className="mt-3" size="sm" variant="outline" disabled={query.isFetching} onClick={() => void query.refetch()}><RefreshCw />重新加载</Button></div> : empty ? <p className="mt-5 text-sm text-muted-foreground">{emptyText}</p> : <div className="mt-5">{children}</div>}
    </section>
  )
}

function Summary({ label, value }: { label: string; value: string }) {
  return <div className="rounded-md bg-muted/40 p-4"><p className="text-xs text-muted-foreground">{label}</p><p className="mt-2 text-xl font-semibold">{value}</p></div>
}

function Row({ label, value }: { label: string; value: string }) {
  return <div className="flex justify-between gap-4"><dt className="text-muted-foreground">{label}</dt><dd>{value}</dd></div>
}
