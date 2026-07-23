import { useQuery } from "@tanstack/react-query"
import {
  Activity,
  Bell,
  BriefcaseBusiness,
  CircleAlert,
  Crosshair,
  Database,
  HeartPulse,
  Radar,
  RefreshCw,
  Server,
  ShieldAlert,
  Target,
  type LucideIcon,
} from "lucide-react"
import { useEffect } from "react"

import { useAuth } from "@/features/auth"
import { dashboardGateway } from "@/features/dashboard/gateway"
import type {
  DashboardGateway,
  DashboardSection,
  DashboardSummary,
} from "@/features/dashboard/types"
import { ApiError } from "@/shared/api/client"
import { Badge } from "@/shared/ui/badge"
import { Button } from "@/shared/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/shared/ui/card"
import { PageState } from "@/shared/ui/page-state"
import { Skeleton } from "@/shared/ui/skeleton"

interface MetricDefinition {
  section: keyof DashboardSummary["sections"]
  field: string
  label: string
  icon: LucideIcon
}

const metrics: MetricDefinition[] = [
  { section: "monitoring", field: "active", label: "启用监控", icon: Radar },
  { section: "positions", field: "held", label: "当前持仓", icon: BriefcaseBusiness },
  { section: "signals", field: "today", label: "今日信号", icon: Crosshair },
  { section: "targets", field: "attention", label: "目标关注", icon: Target },
  { section: "jobs", field: "active", label: "活动任务", icon: Activity },
  { section: "notifications", field: "pending", label: "待发通知", icon: Bell },
  { section: "providers", field: "healthy", label: "健康数据源", icon: Server },
  { section: "alerts", field: "unresolved", label: "未解决告警", icon: ShieldAlert },
  { section: "daily_data", field: "committed_count", label: "日线提交", icon: Database },
  { section: "infrastructure", field: "active_workers", label: "活动进程", icon: HeartPulse },
  { section: "system", field: "critical_alerts", label: "严重告警", icon: CircleAlert },
  { section: "quote_batches", field: "valid_count", label: "有效行情", icon: Activity },
]

const healthLabels: Record<DashboardSummary["status"], string> = {
  HEALTHY: "运行正常",
  DEGRADED: "部分降级",
  UNHEALTHY: "运行异常",
}

const sectionStatusLabels: Record<DashboardSection["status"], string> = {
  OK: "正常",
  EMPTY: "暂无数据",
  WAITING: "等待中",
  NON_TRADING_DAY: "非交易日",
  DEGRADED: "部分降级",
  ERROR: "异常",
  TIMEOUT: "超时",
}

function metricValue(section: DashboardSection, field: string) {
  const value = section.data[field]
  return typeof value === "number" && Number.isFinite(value) ? value : null
}

function statusTone(status: DashboardSection["status"]) {
  if (status === "ERROR" || status === "TIMEOUT") {
    return "danger"
  }
  if (status === "DEGRADED" || status === "WAITING") {
    return "warning"
  }
  return "normal"
}

function DashboardSkeleton() {
  return (
    <main className="mx-auto w-full max-w-7xl p-4 sm:p-6" aria-label="仪表盘加载中">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {metrics.map(({ section }) => (
          <Card key={section}>
            <CardHeader>
              <Skeleton className="size-9" />
              <Skeleton className="h-4 w-24" />
            </CardHeader>
            <CardContent>
              <Skeleton className="h-9 w-20" />
            </CardContent>
          </Card>
        ))}
      </div>
    </main>
  )
}

export function DashboardPage({
  gateway = dashboardGateway,
}: {
  gateway?: DashboardGateway
}) {
  const { invalidate } = useAuth()
  const summaryQuery = useQuery({
    queryKey: ["dashboard", "summary"],
    queryFn: () => gateway.loadSummary(),
    refetchInterval: 15_000,
  })

  useEffect(() => {
    if (summaryQuery.error instanceof ApiError && summaryQuery.error.status === 401) {
      invalidate()
    }
  }, [invalidate, summaryQuery.error])

  if (summaryQuery.isPending) {
    return <DashboardSkeleton />
  }

  if (summaryQuery.isError) {
    const code = summaryQuery.error instanceof ApiError
      ? summaryQuery.error.code
      : "DASHBOARD_UNAVAILABLE"
    return (
      <main className="mx-auto grid min-h-[60vh] w-full max-w-7xl place-items-center p-4 sm:p-6">
        <PageState
          state="error"
          title="仪表盘暂时无法读取"
          description="其他页面仍可继续使用。"
          error={{ code }}
          action={{
            label: "重试仪表盘",
            onClick: () => void summaryQuery.refetch(),
          }}
        />
      </main>
    )
  }

  const summary = summaryQuery.data
  const generatedAt = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Shanghai",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(summary.generated_at))

  return (
    <main className="mx-auto w-full max-w-7xl space-y-6 p-4 sm:p-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <Badge
          variant={summary.status === "UNHEALTHY" ? "destructive" : "secondary"}
        >
          {healthLabels[summary.status]}
        </Badge>
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <time dateTime={summary.generated_at}>{generatedAt} 上海时间</time>
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="刷新仪表盘"
            onClick={() => void summaryQuery.refetch()}
            disabled={summaryQuery.isFetching}
          >
            <RefreshCw aria-hidden="true" />
          </Button>
        </div>
      </header>

      <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4" aria-label="系统实时指标">
        {metrics.map(({ section, field, label, icon: Icon }) => {
          const snapshot = summary.sections[section]
          const value = metricValue(snapshot, field)
          const tone = statusTone(snapshot.status)
          return (
            <Card
              className={
                tone === "danger"
                  ? "border-destructive/60"
                  : tone === "warning"
                    ? "border-primary/60"
                    : undefined
              }
              key={`${section}-${field}`}
              aria-label={`${label}：${value ?? "无数据"}，状态${sectionStatusLabels[snapshot.status]}`}
              title={snapshot.error ?? label}
            >
              <CardHeader className="flex-row items-start justify-between">
                <div className="rounded-md bg-muted p-2">
                  <Icon className="size-5" aria-hidden="true" />
                </div>
                <Badge variant={tone === "danger" ? "destructive" : "outline"}>
                  {sectionStatusLabels[snapshot.status]}
                </Badge>
              </CardHeader>
              <CardContent>
                <CardTitle className="text-3xl">{value ?? "—"}</CardTitle>
                <CardDescription className="mt-1">{label}</CardDescription>
              </CardContent>
            </Card>
          )
        })}
      </section>
    </main>
  )
}
