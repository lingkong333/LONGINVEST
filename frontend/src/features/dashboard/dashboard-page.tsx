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
import { Link } from "react-router-dom"
import { Bar, BarChart, CartesianGrid, LabelList, XAxis } from "recharts"

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
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from "@/shared/ui/chart"
import { Progress } from "@/shared/ui/progress"
import { Skeleton } from "@/shared/ui/skeleton"

interface MetricDefinition {
  section: keyof DashboardSummary["sections"]
  field: string
  label: string
  icon: LucideIcon
  href: string
}

const metrics: MetricDefinition[] = [
  { section: "monitoring", field: "active", label: "启用监控", icon: Radar, href: "/monitoring" },
  { section: "positions", field: "held", label: "当前持仓", icon: BriefcaseBusiness, href: "/positions" },
  { section: "signals", field: "today", label: "今日信号", icon: Crosshair, href: "/signals" },
  { section: "targets", field: "attention", label: "目标关注", icon: Target, href: "/targets" },
  { section: "jobs", field: "active", label: "活动任务", icon: Activity, href: "/jobs" },
  { section: "notifications", field: "pending", label: "待发通知", icon: Bell, href: "/notifications" },
  { section: "providers", field: "healthy", label: "健康数据源", icon: Server, href: "/providers" },
  { section: "alerts", field: "unresolved", label: "未解决告警", icon: ShieldAlert, href: "/alerts" },
  { section: "daily_data", field: "committed_count", label: "日线提交", icon: Database, href: "/market-data" },
  { section: "infrastructure", field: "active_workers", label: "活动进程", icon: HeartPulse, href: "/system-status" },
  { section: "system", field: "critical_alerts", label: "严重告警", icon: CircleAlert, href: "/alerts?severity=CRITICAL" },
  { section: "quote_batches", field: "valid_count", label: "有效行情", icon: Activity, href: "/market-data" },
]

const monitoringChartConfig = {
  value: { label: "股票数", color: "var(--chart-1)" },
} satisfies ChartConfig

const signalChartConfig = {
  value: { label: "信号数", color: "var(--chart-2)" },
} satisfies ChartConfig

function numberValue(section: DashboardSection, field: string): number {
  return metricValue(section, field) ?? 0
}

function textValue(section: DashboardSection, field: string): string | null {
  const value = section.data[field]
  return typeof value === "string" && value ? value : null
}

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
        {metrics.map(({ section, field, label, icon: Icon, href }) => {
          const snapshot = summary.sections[section]
          const value = metricValue(snapshot, field)
          const tone = statusTone(snapshot.status)
          return (
            <Link key={`${section}-${field}`} to={href} className="rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" aria-label={`${label}：${value ?? "无数据"}，状态${sectionStatusLabels[snapshot.status]}，查看详情`}>
            <Card
              className={
                tone === "danger"
                  ? "h-full border-destructive/60 transition-colors hover:bg-muted/40"
                  : tone === "warning"
                    ? "h-full border-primary/60 transition-colors hover:bg-muted/40"
                    : "h-full transition-colors hover:bg-muted/40"
              }
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
            </Link>
          )
        })}
      </section>

      <section className="grid gap-4 lg:grid-cols-2" aria-label="监控和信号图表">
        <Card>
          <CardHeader className="flex-row items-start justify-between gap-3"><div><CardTitle>监控覆盖情况</CardTitle><CardDescription>已启用股票中有多少已经产生当前状态</CardDescription></div><Button asChild variant="ghost"><Link to="/monitoring">查看监控</Link></Button></CardHeader>
          <CardContent>
            <ChartContainer config={monitoringChartConfig} className="h-64 w-full aspect-auto">
              <BarChart accessibilityLayer data={[
                { label: "启用", value: numberValue(summary.sections.monitoring, "active") },
                { label: "已有状态", value: numberValue(summary.sections.monitoring, "with_current_state") },
                { label: "缺少状态", value: numberValue(summary.sections.monitoring, "missing_state") },
              ]} margin={{ top: 24, right: 12, left: 12, bottom: 4 }}>
                <CartesianGrid vertical={false} />
                <XAxis dataKey="label" tickLine={false} axisLine={false} />
                <ChartTooltip cursor={false} content={<ChartTooltipContent hideLabel />} />
                <Bar dataKey="value" fill="var(--color-value)" radius={4}><LabelList dataKey="value" position="top" /></Bar>
              </BarChart>
            </ChartContainer>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex-row items-start justify-between gap-3"><div><CardTitle>信号区间分布</CardTitle><CardDescription>当前监控股票所处的高低价格区间</CardDescription></div><Button asChild variant="ghost"><Link to="/signals">查看信号</Link></Button></CardHeader>
          <CardContent>
            <ChartContainer config={signalChartConfig} className="h-64 w-full aspect-auto">
              <BarChart accessibilityLayer data={[
                { label: "低位", value: numberValue(summary.sections.signals, "low_zone") },
                { label: "高位", value: numberValue(summary.sections.signals, "high_zone") },
                { label: "今日变化", value: numberValue(summary.sections.signals, "today") },
              ]} margin={{ top: 24, right: 12, left: 12, bottom: 4 }}>
                <CartesianGrid vertical={false} />
                <XAxis dataKey="label" tickLine={false} axisLine={false} />
                <ChartTooltip cursor={false} content={<ChartTooltipContent hideLabel />} />
                <Bar dataKey="value" fill="var(--color-value)" radius={4}><LabelList dataKey="value" position="top" /></Bar>
              </BarChart>
            </ChartContainer>
          </CardContent>
        </Card>
      </section>

      <Card aria-label="最近交易日日线完成情况">
        <CardHeader className="flex-row items-start justify-between gap-3"><div><CardTitle>最近交易日日线</CardTitle><CardDescription>系统在确认交易日 17:00 自动创建全市场任务，停机缺口会在后台恢复后补齐。</CardDescription></div><Button asChild variant="outline"><Link to="/market-data">查看日线批次</Link></Button></CardHeader>
        <CardContent className="flex flex-col gap-3">
          {(() => {
            const daily = summary.sections.daily_data
            const committed = numberValue(daily, "committed_count")
            const expected = numberValue(daily, "expected_count")
            const missing = numberValue(daily, "missing_count")
            const failed = numberValue(daily, "failed_count")
            const status = textValue(daily, "status") ?? sectionStatusLabels[daily.status]
            const progress = expected > 0 ? Math.min(100, Math.round(committed / expected * 100)) : 0
            return <><div className="flex flex-wrap items-end justify-between gap-3"><div><p className="text-sm text-muted-foreground">交易日</p><p className="text-xl font-semibold">{textValue(daily, "trading_date") ?? "暂无批次"}</p></div><Badge variant={failed || missing ? "destructive" : "secondary"}>{status}</Badge></div><Progress value={progress} aria-label={`日线完成进度 ${progress}%`} /><div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4"><p><span className="block text-muted-foreground">已提交</span><strong>{committed} / {expected || "—"}</strong></p><p><span className="block text-muted-foreground">完成度</span><strong>{progress}%</strong></p><p><span className="block text-muted-foreground">缺失</span><strong>{missing}</strong></p><p><span className="block text-muted-foreground">失败</span><strong>{failed}</strong></p></div></>
          })()}
        </CardContent>
      </Card>
    </main>
  )
}
