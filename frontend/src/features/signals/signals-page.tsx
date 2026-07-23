import { useQuery } from "@tanstack/react-query"
import {
  Bell,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  Clock3,
  Crosshair,
  History,
  RefreshCw,
  Route,
  ShieldAlert,
} from "lucide-react"
import { useEffect, useState } from "react"

import { useAuth } from "@/features/auth"
import { signalsGateway } from "@/features/signals/gateway"
import type {
  EvaluationReason,
  EvaluationResult,
  PageResult,
  SignalEvaluation,
  SignalEventItem,
  SignalEventPage,
  SignalState,
  SignalsGateway,
  SignalZone,
} from "@/features/signals/types"
import { ApiError } from "@/shared/api/client"
import { Button } from "@/shared/ui/button"

const PAGE_SIZE = 20

const zoneLabels: Record<SignalZone, string> = {
  UNKNOWN: "尚未判断",
  STRONG_LOW: "强低位",
  LOW: "低位",
  NORMAL: "正常区间",
  HIGH: "高位",
  STRONG_HIGH: "强高位",
}

const reasonLabels: Record<EvaluationReason, string> = {
  SCHEDULED_QUOTE: "定时行情",
  MANUAL_CHECK: "人工检查",
  TARGET_ACTIVATED: "目标启用",
  POSITION_BECAME_HOLDING: "转为持仓",
  DATA_CORRECTION: "数据修正",
  STATE_RESET: "状态重置",
  RECOVERY_REEVALUATION: "恢复后重评",
}

const resultLabels: Record<EvaluationResult, string> = {
  APPLIED: "状态已变化",
  UNCHANGED: "仍在同一区间",
  SKIPPED: "已跳过",
  SUPERSEDED: "已过期",
}

const notificationLabels: Record<string, string> = {
  ELIGIBLE: "等待投递",
  SUPPRESSED: "已抑制",
  DISPATCHED: "投递中",
  PARTIAL: "部分送达",
  DELIVERED: "已送达",
  FAILED: "投递失败",
  CANCELED: "已取消",
}

const deliveryLabels: Record<string, string> = {
  PENDING: "等待发送",
  SENDING: "发送中",
  SENT: "已发送",
  RETRY_WAIT: "等待重试",
  OUTCOME_UNKNOWN: "结果未知",
  FAILED: "发送失败",
  CANCELED: "已取消",
  SKIPPED_DISABLED: "渠道未启用",
  SKIPPED_INELIGIBLE: "不符合条件",
}

const channelLabels: Record<string, string> = {
  WECOM: "企业微信",
  EMAIL: "电子邮件",
}

type SectionKey = "states" | "events" | "evaluations"

function translated(labels: Record<string, string>, value: string | null | undefined) {
  if (!value) {
    return "暂无"
  }
  return labels[value] ?? `未知（${value}）`
}

function formatShanghaiTime(value: string | null | undefined) {
  if (!value) {
    return "暂无时间"
  }
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return "时间格式异常"
  }
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

function shortId(value: string) {
  return value.length > 12 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value
}

function zoneTone(zone: SignalZone) {
  if (zone === "STRONG_LOW" || zone === "LOW") {
    return "border-emerald-400/40 bg-emerald-500/10 text-emerald-700"
  }
  if (zone === "STRONG_HIGH" || zone === "HIGH") {
    return "border-rose-400/40 bg-rose-500/10 text-rose-700"
  }
  if (zone === "UNKNOWN") {
    return "border-border bg-muted text-muted-foreground"
  }
  return "border-sky-400/40 bg-sky-500/10 text-sky-700"
}

function ZoneBadge({ zone }: { zone: SignalZone }) {
  return (
    <span className={`inline-flex rounded-full border px-2.5 py-1 text-xs font-medium ${zoneTone(zone)}`}>
      {zoneLabels[zone]}
    </span>
  )
}

function SectionFailure({
  title,
  error,
  retry,
}: {
  title: string
  error: unknown
  retry(): void
}) {
  const code = error instanceof ApiError ? error.code : "SIGNALS_UNAVAILABLE"
  return (
    <div className="flex min-h-52 flex-col items-center justify-center gap-3 rounded-lg border border-destructive/30 bg-destructive/5 p-8 text-center">
      <CircleAlert className="text-destructive" aria-hidden="true" />
      <strong>{title}暂时无法读取</strong>
      <code className="text-xs text-muted-foreground">{code}</code>
      <Button variant="outline" onClick={retry}>
        <RefreshCw aria-hidden="true" />
        重新加载
      </Button>
    </div>
  )
}

function SectionLoading({ label }: { label: string }) {
  return (
    <div
      className="grid min-h-52 place-items-center rounded-lg border border-border bg-card"
      aria-label={`${label}加载中`}
    >
      <RefreshCw className="animate-spin text-muted-foreground" aria-hidden="true" />
    </div>
  )
}

function EmptyState({ title, description }: { title: string; description: string }) {
  return (
    <div className="grid min-h-52 place-items-center rounded-lg border border-dashed border-border bg-muted/20 p-8 text-center">
      <div>
        <History className="mx-auto mb-3 text-muted-foreground" aria-hidden="true" />
        <h2 className="font-semibold">{title}</h2>
        <p className="mt-1 text-sm text-muted-foreground">{description}</p>
      </div>
    </div>
  )
}

function Pagination({
  page,
  pageSize,
  total,
  onChange,
}: {
  page: number
  pageSize: number
  total: number
  onChange(page: number): void
}) {
  const pages = Math.max(1, Math.ceil(total / pageSize))
  return (
    <nav className="flex items-center justify-between border-t border-border pt-4" aria-label="分页">
      <span className="text-xs text-muted-foreground">共 {total} 条，第 {page} / {pages} 页</span>
      <div className="flex gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={page <= 1}
          onClick={() => onChange(page - 1)}
        >
          <ChevronLeft aria-hidden="true" />
          上一页
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={page >= pages}
          onClick={() => onChange(page + 1)}
        >
          下一页
          <ChevronRight aria-hidden="true" />
        </Button>
      </div>
    </nav>
  )
}

function StatesSection({
  setPage,
  query,
}: {
  setPage(page: number): void
  query: {
    data?: PageResult<SignalState>
    isPending: boolean
    isError: boolean
    error: unknown
    refetch(): unknown
  }
}) {
  const [zone, setZone] = useState("")
  const [search, setSearch] = useState("")
  if (query.isPending) {
    return <SectionLoading label="当前状态" />
  }
  if (query.isError || !query.data) {
    return (
      <SectionFailure
        title="当前状态"
        error={query.error}
        retry={() => void query.refetch()}
      />
    )
  }

  const keyword = search.trim().toLowerCase()
  const items = query.data.items.filter((item) => (
    (!zone || item.zone === zone)
    && (!keyword || item.subscription_id.toLowerCase().includes(keyword))
  ))

  return (
    <section aria-label="当前信号状态" className="space-y-4">
      <div className="flex flex-wrap gap-3 rounded-lg border border-border bg-card p-3">
        <input
          className="h-9 min-w-60 flex-1 rounded-md border border-input bg-background px-3 text-sm outline-none focus:ring-2 focus:ring-ring/40"
          aria-label="搜索订阅编号"
          placeholder="搜索订阅编号"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <select
          className="h-9 rounded-md border border-input bg-background px-3 text-sm"
          aria-label="按信号区间筛选"
          value={zone}
          onChange={(event) => setZone(event.target.value)}
        >
          <option value="">全部区间</option>
          {Object.entries(zoneLabels).map(([value, label]) => (
            <option value={value} key={value}>{label}</option>
          ))}
        </select>
      </div>
      {query.data.items.length === 0 ? (
        <EmptyState title="暂无当前状态" description="首次正式判断后，信号状态会显示在这里。" />
      ) : items.length === 0 ? (
        <EmptyState title="没有符合条件的状态" description="请调整搜索或区间筛选。" />
      ) : (
        <div className="grid gap-3 xl:grid-cols-2">
          {items.map((item) => (
            <article className="rounded-lg border border-border bg-card p-4 shadow-sm" key={item.subscription_id}>
              <div className="flex items-start justify-between gap-3">
                <div>
                  <p className="text-xs text-muted-foreground">订阅</p>
                  <code title={item.subscription_id} className="text-sm">{shortId(item.subscription_id)}</code>
                </div>
                <ZoneBadge zone={item.zone} />
              </div>
              <div className="mt-5 grid grid-cols-2 gap-4 text-sm">
                <div>
                  <span className="text-muted-foreground">最新价格</span>
                  <strong className="mt-1 block text-lg">{item.last_price ? `¥ ${item.last_price}` : "暂无"}</strong>
                </div>
                <div>
                  <span className="text-muted-foreground">状态版本</span>
                  <strong className="mt-1 block text-lg">第 {item.version} 版</strong>
                </div>
              </div>
              <div className="mt-4 border-t border-border pt-3 text-xs text-muted-foreground">
                行情时间：{formatShanghaiTime(item.last_price_at)}
              </div>
            </article>
          ))}
        </div>
      )}
      <Pagination {...query.data} onChange={setPage} />
    </section>
  )
}

function TargetSnapshot({ event }: { event: SignalEventItem }) {
  return (
    <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
      <span>强低 {event.targets.low_strong}</span>
      <span>低位 {event.targets.low_watch}</span>
      <span>高位 {event.targets.high_watch}</span>
      <span>强高 {event.targets.high_strong}</span>
    </div>
  )
}

function EventsSection({
  setPage,
  query,
}: {
  setPage(page: number): void
  query: {
    data?: SignalEventPage
    isPending: boolean
    isError: boolean
    error: unknown
    refetch(): unknown
  }
}) {
  const [reason, setReason] = useState("")
  const [eligibility, setEligibility] = useState("")
  if (query.isPending) {
    return <SectionLoading label="信号事件" />
  }
  if (query.isError || !query.data) {
    return (
      <SectionFailure
        title="信号事件"
        error={query.error}
        retry={() => void query.refetch()}
      />
    )
  }
  const items = query.data.items.filter((item) => (
    (!reason || item.reason === reason)
    && (!eligibility || String(item.notification_eligible) === eligibility)
  ))

  return (
    <section aria-label="信号事件" className="space-y-4">
      {query.data.warningCodes.length > 0 ? (
        <aside className="flex items-start gap-2 rounded-lg border border-amber-400/40 bg-amber-500/10 p-3 text-sm" role="status">
          <ShieldAlert className="mt-0.5 size-4 shrink-0 text-amber-700" aria-hidden="true" />
          <span>通知投递数据暂时不完整，信号事件仍可正常查看。</span>
        </aside>
      ) : null}
      <div className="flex flex-wrap gap-3 rounded-lg border border-border bg-card p-3">
        <select
          className="h-9 rounded-md border border-input bg-background px-3 text-sm"
          aria-label="按判断原因筛选事件"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
        >
          <option value="">全部原因</option>
          {Object.entries(reasonLabels).map(([value, label]) => (
            <option value={value} key={value}>{label}</option>
          ))}
        </select>
        <select
          className="h-9 rounded-md border border-input bg-background px-3 text-sm"
          aria-label="按通知资格筛选"
          value={eligibility}
          onChange={(event) => setEligibility(event.target.value)}
        >
          <option value="">全部通知资格</option>
          <option value="true">符合通知条件</option>
          <option value="false">不发送通知</option>
        </select>
      </div>
      {query.data.items.length === 0 ? (
        <EmptyState title="暂无信号事件" description="只有真实区间转换才会形成信号事件。" />
      ) : items.length === 0 ? (
        <EmptyState title="没有符合条件的事件" description="请调整事件筛选条件。" />
      ) : (
        <div className="space-y-3">
          {items.map((item) => (
            <article className="rounded-lg border border-border bg-card p-4 shadow-sm" key={item.id}>
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-center gap-2">
                  <ZoneBadge zone={item.before_zone} />
                  <Route className="size-4 text-muted-foreground" aria-hidden="true" />
                  <ZoneBadge zone={item.after_zone} />
                </div>
                <time className="text-xs text-muted-foreground" dateTime={item.price_at}>
                  {formatShanghaiTime(item.price_at)}
                </time>
              </div>
              <div className="mt-4 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
                <div>
                  <span className="text-xs text-muted-foreground">价格与原因</span>
                  <strong className="mt-1 block">¥ {item.price}</strong>
                  <small>{reasonLabels[item.reason]}</small>
                </div>
                <div>
                  <span className="text-xs text-muted-foreground">目标快照 · 第 {item.target_version} 版</span>
                  <div className="mt-1"><TargetSnapshot event={item} /></div>
                </div>
                <div>
                  <span className="text-xs text-muted-foreground">持仓与通知资格</span>
                  <strong className="mt-1 block">{item.position_status === "HOLDING" ? "当前持仓" : "当前未持仓"}</strong>
                  <small>{item.notification_eligible ? "符合通知条件" : `不发送：${item.suppression_reason ?? "不符合规则"}`}</small>
                </div>
                <div>
                  <span className="text-xs text-muted-foreground">投递结果</span>
                  <strong className="mt-1 block">
                    {item.notificationStatus
                      ? translated(notificationLabels, item.notificationStatus)
                      : item.notification_eligible ? "等待创建通知" : "无需投递"}
                  </strong>
                  {item.deliveries.length > 0 ? item.deliveries.map((delivery) => (
                    <small className="block" key={delivery.id}>
                      {translated(channelLabels, delivery.channel)}：{translated(deliveryLabels, delivery.status)}
                      {delivery.errorCode ? `（${delivery.errorCode}）` : ""}
                    </small>
                  )) : null}
                </div>
              </div>
            </article>
          ))}
        </div>
      )}
      <Pagination {...query.data} onChange={setPage} />
    </section>
  )
}

function EvaluationsSection({
  setPage,
  query,
}: {
  setPage(page: number): void
  query: {
    data?: PageResult<SignalEvaluation>
    isPending: boolean
    isError: boolean
    error: unknown
    refetch(): unknown
  }
}) {
  const [result, setResult] = useState("")
  const items = query.data?.items.filter((item) => !result || item.result === result) ?? []
  if (query.isPending) {
    return <SectionLoading label="判断记录" />
  }
  if (query.isError || !query.data) {
    return (
      <SectionFailure
        title="判断记录"
        error={query.error}
        retry={() => void query.refetch()}
      />
    )
  }
  return (
    <section aria-label="信号判断记录" className="space-y-4">
      <div className="flex rounded-lg border border-border bg-card p-3">
        <select
          className="h-9 rounded-md border border-input bg-background px-3 text-sm"
          aria-label="按判断结果筛选"
          value={result}
          onChange={(event) => setResult(event.target.value)}
        >
          <option value="">全部判断结果</option>
          {Object.entries(resultLabels).map(([value, label]) => (
            <option value={value} key={value}>{label}</option>
          ))}
        </select>
      </div>
      {query.data.items.length === 0 ? (
        <EmptyState title="暂无判断记录" description="每次正式比较都会保留在这里，包括状态未变化和跳过。" />
      ) : items.length === 0 ? (
        <EmptyState title="没有符合条件的判断" description="请调整判断结果筛选。" />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border bg-card">
          <table className="w-full min-w-[850px] text-left text-sm">
            <thead className="border-b border-border bg-muted/40 text-xs text-muted-foreground">
              <tr>
                <th className="p-3 font-medium">判断时间</th>
                <th className="p-3 font-medium">区间变化</th>
                <th className="p-3 font-medium">结果</th>
                <th className="p-3 font-medium">价格</th>
                <th className="p-3 font-medium">原因</th>
                <th className="p-3 font-medium">附加信息</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {items.map((item) => (
                <tr key={item.id} className="align-top hover:bg-muted/20">
                  <td className="p-3">
                    <time dateTime={item.created_at}>{formatShanghaiTime(item.created_at)}</time>
                    <code className="mt-1 block text-xs text-muted-foreground" title={item.subscription_id}>
                      {shortId(item.subscription_id)}
                    </code>
                  </td>
                  <td className="p-3">{zoneLabels[item.before_zone]} → {zoneLabels[item.after_zone]}</td>
                  <td className="p-3 font-medium">{resultLabels[item.result]}</td>
                  <td className="p-3">{item.price ? `¥ ${item.price}` : "无有效价格"}</td>
                  <td className="p-3">{reasonLabels[item.reason]}</td>
                  <td className="p-3 text-xs text-muted-foreground">
                    {item.skip_code ? <span className="block">跳过原因：{item.skip_code}</span> : null}
                    {item.hysteresis_applied ? <span className="block">已应用区间缓冲</span> : null}
                    {item.used_stale_target ? <span className="block text-amber-700">使用了待更新目标</span> : null}
                    {!item.skip_code && !item.hysteresis_applied && !item.used_stale_target ? "无" : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Pagination {...query.data} onChange={setPage} />
    </section>
  )
}

export function SignalsPage({
  gateway = signalsGateway,
}: {
  gateway?: SignalsGateway
}) {
  const { invalidate } = useAuth()
  const [section, setSection] = useState<SectionKey>("states")
  const [statePage, setStatePage] = useState(1)
  const [eventPage, setEventPage] = useState(1)
  const [evaluationPage, setEvaluationPage] = useState(1)
  const statesQuery = useQuery({
    queryKey: ["signals", "states", statePage],
    queryFn: () => gateway.loadStates(statePage, PAGE_SIZE),
  })
  const eventsQuery = useQuery({
    queryKey: ["signals", "events", eventPage],
    queryFn: () => gateway.loadEvents(eventPage, PAGE_SIZE),
  })
  const evaluationsQuery = useQuery({
    queryKey: ["signals", "evaluations", evaluationPage],
    queryFn: () => gateway.loadEvaluations(evaluationPage, PAGE_SIZE),
  })

  useEffect(() => {
    const errors = [statesQuery.error, eventsQuery.error, evaluationsQuery.error]
    if (errors.some((error) => error instanceof ApiError && error.status === 401)) {
      invalidate()
    }
  }, [
    evaluationsQuery.error,
    eventsQuery.error,
    invalidate,
    statesQuery.error,
  ])

  const sections = [
    { key: "states" as const, label: "当前状态", icon: Crosshair, count: statesQuery.data?.total },
    { key: "events" as const, label: "信号事件", icon: Bell, count: eventsQuery.data?.total },
    { key: "evaluations" as const, label: "判断记录", icon: Clock3, count: evaluationsQuery.data?.total },
  ]

  return (
    <main className="mx-auto w-full max-w-[1500px] space-y-6 p-4 md:p-6">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-border pb-5">
        <div>
          <p className="mb-1 flex items-center gap-2 text-xs font-semibold text-muted-foreground">
            <CheckCircle2 className="size-4 text-emerald-600" aria-hidden="true" />
            价格区间判断
          </p>
          <h1 className="text-2xl font-semibold">信号中心</h1>
          <p className="mt-1 text-sm text-muted-foreground">查看当前区间、真实转换和每一次正式判断。</p>
        </div>
        <Button
          variant="outline"
          aria-label="刷新信号中心"
          onClick={() => {
            void statesQuery.refetch()
            void eventsQuery.refetch()
            void evaluationsQuery.refetch()
          }}
          disabled={statesQuery.isFetching || eventsQuery.isFetching || evaluationsQuery.isFetching}
        >
          <RefreshCw aria-hidden="true" />
          刷新
        </Button>
      </header>

      <nav className="grid gap-2 sm:grid-cols-3" aria-label="信号中心分区">
        {sections.map(({ key, label, icon: Icon, count }) => (
          <button
            type="button"
            key={key}
            aria-current={section === key ? "page" : undefined}
            className={`flex items-center justify-between rounded-lg border px-4 py-3 text-left transition-colors ${
              section === key
                ? "border-foreground bg-foreground text-background"
                : "border-border bg-card hover:bg-muted/50"
            }`}
            onClick={() => setSection(key)}
          >
            <span className="flex items-center gap-2 font-medium"><Icon className="size-4" aria-hidden="true" />{label}</span>
            <strong className="text-sm tabular-nums">{count ?? "—"}</strong>
          </button>
        ))}
      </nav>

      {section === "states" ? (
        <StatesSection setPage={setStatePage} query={statesQuery} />
      ) : section === "events" ? (
        <EventsSection setPage={setEventPage} query={eventsQuery} />
      ) : (
        <EvaluationsSection
          setPage={setEvaluationPage}
          query={evaluationsQuery}
        />
      )}
    </main>
  )
}
