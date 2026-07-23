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
import { Button } from "@/shared/ui/button"

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
    <main className="dashboard-page" aria-label="仪表盘加载中">
      <div className="dashboard-loading">
        {metrics.map(({ section }) => <span key={section} />)}
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
      <main className="dashboard-page dashboard-page--error">
        <CircleAlert aria-hidden="true" />
        <code>{code}</code>
        <Button
          variant="outline"
          size="icon"
          aria-label="重试仪表盘"
          onClick={() => void summaryQuery.refetch()}
        >
          <RefreshCw aria-hidden="true" />
        </Button>
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
    <main className="dashboard-page">
      <header className="dashboard-header">
        <div className={`dashboard-health dashboard-health--${summary.status.toLowerCase()}`}>
          <span aria-hidden="true" />
          <strong>{healthLabels[summary.status]}</strong>
        </div>
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
      </header>

      <section className="dashboard-metrics" aria-label="系统实时指标">
        {metrics.map(({ section, field, label, icon: Icon }) => {
          const snapshot = summary.sections[section]
          const value = metricValue(snapshot, field)
          const tone = statusTone(snapshot.status)
          return (
            <article
              className={`metric-card metric-card--${tone}`}
              key={`${section}-${field}`}
              aria-label={`${label}：${value ?? "无数据"}，状态${sectionStatusLabels[snapshot.status]}`}
              title={snapshot.error ?? label}
            >
              <div className="metric-card__icon"><Icon aria-hidden="true" /></div>
              <strong>{value ?? "—"}</strong>
              <span>{label}</span>
              <i aria-hidden="true" />
            </article>
          )
        })}
      </section>
    </main>
  )
}
