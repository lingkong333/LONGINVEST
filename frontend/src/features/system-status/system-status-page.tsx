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
import { Alert, AlertDescription } from "@/shared/ui/alert"
import { Badge } from "@/shared/ui/badge"
import { Button } from "@/shared/ui/button"
import {
  Card,
  CardContent,
  CardHeader,
} from "@/shared/ui/card"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/shared/ui/table"

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
            <article key={`${component.category}-${component.name}`} className="rounded-md bg-muted/40 p-4">
              <div className="flex items-start justify-between gap-3">
                <div><h3 className="font-medium">{component.name}</h3><p className="mt-1 text-xs text-muted-foreground">{component.category} · {component.source}</p></div>
                <Badge variant="outline">{statusText(component.status)}</Badge>
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
              <Table><TableHeader><TableRow><TableHead>名称</TableHead><TableHead>状态</TableHead><TableHead>等待任务</TableHead><TableHead>活动 Worker</TableHead></TableRow></TableHeader><TableBody>{query.data.queues.map((queue) => <TableRow key={queue.name}><TableCell>{queue.name}</TableCell><TableCell><Badge variant="outline">{statusText(queue.status)}</Badge></TableCell><TableCell>{queue.depth}</TableCell><TableCell>{queue.activeWorkers}</TableCell></TableRow>)}</TableBody></Table>
            )}
          </div>
          <div>
            <h3 className="mb-3 text-sm font-semibold">Worker</h3>
            {query.data.workers.length === 0 ? <p className="text-sm text-muted-foreground">暂无 Worker</p> : (
              <Table><TableHeader><TableRow><TableHead>标识</TableHead><TableHead>队列</TableHead><TableHead>状态</TableHead><TableHead>心跳</TableHead></TableRow></TableHeader><TableBody>{query.data.workers.map((worker) => <TableRow key={worker.workerId}><TableCell className="font-mono text-xs">{worker.workerId}</TableCell><TableCell>{worker.queue}</TableCell><TableCell><Badge variant="outline">{worker.status}</Badge></TableCell><TableCell>{formatTime(worker.heartbeatAt)}</TableCell></TableRow>)}</TableBody></Table>
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
          <article className="rounded-md bg-muted/40 p-4">
            <div className="flex justify-between gap-3"><h3 className="font-medium">自动调度</h3><Badge variant="outline">{statusText(query.data.scheduler.status)}</Badge></div>
            <dl className="mt-4 grid gap-2 text-sm"><Row label="扫描间隔" value={`${query.data.scheduler.scanIntervalSeconds} 秒`} /><Row label="最近扫描" value={formatTime(query.data.scheduler.lastScanAt)} /><Row label="自动调度" value={query.data.scheduler.automaticSchedulingPaused ? "已暂停" : "运行中"} /></dl>
            {query.data.scheduler.pauseReason ? <p className="mt-3 text-sm text-destructive">{query.data.scheduler.pauseReason}</p> : null}
          </article>
          <article className="rounded-md bg-muted/40 p-4">
            <div className="flex justify-between gap-3"><h3 className="font-medium">系统时钟</h3><Badge variant="outline">{statusText(query.data.clock.status)}</Badge></div>
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
        <div>
          <Table>
            <TableHeader><TableRow><TableHead>计划时间</TableHead><TableHead>类型</TableHead><TableHead>定义</TableHead><TableHead>状态</TableHead><TableHead>说明</TableHead></TableRow></TableHeader>
            <TableBody>{query.data.items.map((item) => <TableRow key={item.occurrenceId}><TableCell>{formatTime(item.scheduledAt)}</TableCell><TableCell>{occurrenceLabels[item.occurrenceType] ?? item.occurrenceType}</TableCell><TableCell>{item.definitionId}</TableCell><TableCell><Badge variant="outline">{occurrenceLabels[item.status] ?? item.status}</Badge></TableCell><TableCell>{item.missedReason ?? "暂无"}</TableCell></TableRow>)}</TableBody>
          </Table>
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
    <Card role="region" aria-label={title}>
      <CardHeader className="flex-row items-center justify-between">
        <div className="flex items-center gap-3"><span className="text-muted-foreground">{icon}</span><h2 className="font-semibold">{title}</h2></div>
        <Button size="icon-sm" variant="outline" aria-label={refreshLabel} title={refreshLabel} disabled={query.isFetching} onClick={() => void query.refetch()}><RefreshCw data-icon="icon" className={query.isFetching ? "animate-spin" : undefined} /></Button>
      </CardHeader>
      <CardContent>
        {query.isPending ? <p className="text-sm text-muted-foreground" role="status">正在读取{title}…</p> : query.isError ? <Alert variant="destructive"><AlertDescription className="flex items-center justify-between gap-3"><span>{errorDiagnostic(query.error, errorCode)}</span><Button size="sm" variant="outline" disabled={query.isFetching} onClick={() => void query.refetch()}><RefreshCw data-icon="inline-start" />重新加载</Button></AlertDescription></Alert> : empty ? <p className="text-sm text-muted-foreground">{emptyText}</p> : children}
      </CardContent>
    </Card>
  )
}

function Summary({ label, value }: { label: string; value: string }) {
  return <div className="border-l-2 border-primary pl-4"><p className="text-xs text-muted-foreground">{label}</p><p className="mt-2 text-xl font-semibold">{value}</p></div>
}

function Row({ label, value }: { label: string; value: string }) {
  return <div className="flex justify-between gap-4"><dt className="text-muted-foreground">{label}</dt><dd>{value}</dd></div>
}
