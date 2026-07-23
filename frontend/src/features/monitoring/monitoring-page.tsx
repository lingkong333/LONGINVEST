import { useQuery } from "@tanstack/react-query"
import {
  BriefcaseBusiness,
  CircleAlert,
  Radar,
  RefreshCw,
  Search,
  TriangleAlert,
} from "lucide-react"
import { useEffect, useMemo, useState } from "react"

import { useAuth } from "@/features/auth"
import { monitoringGateway } from "@/features/monitoring/gateway"
import type {
  MonitoringGateway,
  MonitoringOverviewItem,
} from "@/features/monitoring/types"
import { ApiError } from "@/shared/api/client"
import { Button } from "@/shared/ui/button"
import { Input } from "@/shared/ui/input"

type MonitorFilter = "全部" | "已启用" | "持仓" | "需关注"

const subscriptionLabels: Record<string, string> = {
  ENABLED: "已启用",
  PAUSED: "已暂停",
  CONFIGURING: "待配置",
  ARCHIVED: "已归档",
}

const targetLabels: Record<string, string> = {
  READY: "正常",
  STALE: "已过期",
  MISSING: "缺失",
  CALCULATING: "计算中",
  REVIEW_REQUIRED: "待复核",
  ACTIVATING: "激活中",
  FAILED: "计算失败",
}

const targetModeLabels: Record<string, string> = {
  MANUAL: "手工目标",
  STRATEGY: "策略目标",
}

const zoneLabels: Record<string, string> = {
  UNKNOWN: "未知",
  STRONG_LOW: "强低位",
  LOW: "低位",
  NORMAL: "正常区间",
  HIGH: "高位",
  STRONG_HIGH: "强高位",
}

function translated(mapping: Record<string, string>, value: string | null) {
  if (!value) {
    return "暂无"
  }
  return mapping[value] ?? "未知状态"
}

function isAttention(item: MonitoringOverviewItem) {
  return (
    item.warningCodes.length > 0
    || item.targetStatus === "STALE"
    || item.targetStatus === "MISSING"
    || item.zone === "UNKNOWN"
  )
}

function formatShanghaiTime(value: string | null) {
  if (!value) {
    return "暂无"
  }
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value))
}

function MonitoringSkeleton() {
  return (
    <main className="monitoring-page" aria-label="监控列表加载中">
      <div className="monitoring-skeleton">
        <span />
        <span />
        <span />
        <span />
      </div>
    </main>
  )
}

export function MonitoringPage({
  gateway = monitoringGateway,
}: {
  gateway?: MonitoringGateway
}) {
  const { invalidate } = useAuth()
  const [filter, setFilter] = useState<MonitorFilter>("全部")
  const [search, setSearch] = useState("")
  const [groupFilter, setGroupFilter] = useState("")
  const [modeFilter, setModeFilter] = useState("")
  const [zoneFilter, setZoneFilter] = useState("")
  const overviewQuery = useQuery({
    queryKey: ["monitoring", "overview"],
    queryFn: () => gateway.loadOverview(),
    refetchInterval: 15_000,
  })

  useEffect(() => {
    if (overviewQuery.error instanceof ApiError && overviewQuery.error.status === 401) {
      invalidate()
    }
  }, [invalidate, overviewQuery.error])

  const visibleItems = useMemo(() => {
    if (!overviewQuery.data) {
      return []
    }
    const normalizedSearch = search.trim().toLocaleLowerCase("zh-CN")
    return overviewQuery.data.items.filter((item) => {
      const matchesFilter = filter === "全部"
        || (filter === "已启用" && item.subscriptionStatus === "ENABLED")
        || (filter === "持仓" && item.isHolding)
        || (filter === "需关注" && isAttention(item))
      const matchesSearch = !normalizedSearch
        || item.symbol.toLocaleLowerCase("zh-CN").includes(normalizedSearch)
        || (item.securityName ?? "").toLocaleLowerCase("zh-CN").includes(normalizedSearch)
        || item.groups.some((group) => (
          group.toLocaleLowerCase("zh-CN").includes(normalizedSearch)
        ))
      const matchesGroup = !groupFilter || item.groups.includes(groupFilter)
      const matchesMode = !modeFilter || item.targetMode === modeFilter
      const matchesZone = !zoneFilter || item.zone === zoneFilter
      return (
        matchesFilter
        && matchesSearch
        && matchesGroup
        && matchesMode
        && matchesZone
      )
    })
  }, [filter, groupFilter, modeFilter, overviewQuery.data, search, zoneFilter])

  if (overviewQuery.isPending) {
    return <MonitoringSkeleton />
  }

  if (overviewQuery.isError) {
    const code = overviewQuery.error instanceof ApiError
      ? overviewQuery.error.code
      : "MONITORING_UNAVAILABLE"
    return (
      <main className="monitoring-page monitoring-page--error">
        <CircleAlert aria-hidden="true" />
        <strong>监控列表暂时无法读取</strong>
        <code>{code}</code>
        <Button
          variant="outline"
          aria-label="重新加载监控列表"
          onClick={() => void overviewQuery.refetch()}
        >
          <RefreshCw aria-hidden="true" />
          重试
        </Button>
      </main>
    )
  }

  const overview = overviewQuery.data
  const enabledCount = overview.items.filter(
    (item) => item.subscriptionStatus === "ENABLED",
  ).length
  const holdingCount = overview.items.filter((item) => item.isHolding).length
  const attentionCount = overview.items.filter(isAttention).length
  const groupOptions = Array.from(
    new Set(overview.items.flatMap((item) => item.groups)),
  ).sort((left, right) => left.localeCompare(right, "zh-CN"))

  return (
    <main className="monitoring-page">
      <header className="monitoring-header">
        <div>
          <span className="monitoring-header__mark"><Radar aria-hidden="true" /></span>
          <div>
            <p>实时监控</p>
            <h1>监控列表</h1>
          </div>
        </div>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="刷新监控列表"
          onClick={() => void overviewQuery.refetch()}
          disabled={overviewQuery.isFetching}
        >
          <RefreshCw aria-hidden="true" />
        </Button>
      </header>

      <section className="monitoring-summary" aria-label="监控概况">
        <div><strong>{overview.items.length}</strong><span>全部股票</span></div>
        <div><strong>{enabledCount}</strong><span>已启用</span></div>
        <div><strong>{holdingCount}</strong><span>当前持仓</span></div>
        <div className={attentionCount > 0 ? "is-attention" : ""}>
          <strong>{attentionCount}</strong><span>需要关注</span>
        </div>
      </section>

      {overview.warningCodes.length > 0 ? (
        <aside className="monitoring-warning" role="status">
          <TriangleAlert aria-hidden="true" />
          <span>部分辅助数据暂不可用，股票订阅仍可正常查看。</span>
          <code>{overview.warningCodes.join(" · ")}</code>
        </aside>
      ) : null}

      <section className="monitoring-toolbar" aria-label="监控筛选">
        <div className="monitoring-filters">
          {(["全部", "已启用", "持仓", "需关注"] as const).map((option) => (
            <button
              type="button"
              key={option}
              className={filter === option ? "is-active" : ""}
              aria-pressed={filter === option}
              onClick={() => setFilter(option)}
            >
              {option}
            </button>
          ))}
        </div>
        <div className="monitoring-selects">
          <label>
            <span className="sr-only">按分组筛选</span>
            <select
              aria-label="按分组筛选"
              value={groupFilter}
              onChange={(event) => setGroupFilter(event.target.value)}
            >
              <option value="">全部分组</option>
              {groupOptions.map((group) => (
                <option value={group} key={group}>{group}</option>
              ))}
            </select>
          </label>
          <label>
            <span className="sr-only">按目标模式筛选</span>
            <select
              aria-label="按目标模式筛选"
              value={modeFilter}
              onChange={(event) => setModeFilter(event.target.value)}
            >
              <option value="">全部模式</option>
              <option value="MANUAL">手工目标</option>
              <option value="STRATEGY">策略目标</option>
            </select>
          </label>
          <label>
            <span className="sr-only">按价格区间筛选</span>
            <select
              aria-label="按价格区间筛选"
              value={zoneFilter}
              onChange={(event) => setZoneFilter(event.target.value)}
            >
              <option value="">全部区间</option>
              {Object.entries(zoneLabels).map(([value, label]) => (
                <option value={value} key={value}>{label}</option>
              ))}
            </select>
          </label>
        </div>
        <label className="monitoring-search">
          <Search aria-hidden="true" />
          <span className="sr-only">搜索股票、名称或分组</span>
          <Input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="搜索股票、名称或分组"
          />
        </label>
      </section>

      {overview.items.length === 0 ? (
        <section className="monitoring-empty">
          <Radar aria-hidden="true" />
          <h2>还没有监控股票</h2>
          <p>创建监控订阅后，股票会显示在这里。</p>
        </section>
      ) : visibleItems.length === 0 ? (
        <section className="monitoring-empty">
          <Search aria-hidden="true" />
          <h2>没有符合条件的股票</h2>
          <p>请调整筛选条件或搜索内容。</p>
        </section>
      ) : (
        <section className="monitoring-list" aria-label="监控股票">
          <div className="monitoring-list__head" aria-hidden="true">
            <span>股票</span>
            <span>分组</span>
            <span>状态</span>
            <span>监控设置</span>
            <span>区间与价格</span>
          </div>
          {visibleItems.map((item) => (
            <article className="monitoring-row" key={item.subscriptionId}>
              <div className="monitoring-stock">
                <strong>{item.securityName ?? "名称暂缺"}</strong>
                <code>{item.symbol}</code>
              </div>
              <div className="monitoring-groups">
                {item.groups.length > 0
                  ? item.groups.map((group) => <span key={group}>{group}</span>)
                  : <span>未分组</span>}
              </div>
              <div className="monitoring-state">
                <span className={`status-dot status-dot--${item.subscriptionStatus.toLowerCase()}`}>
                  {translated(subscriptionLabels, item.subscriptionStatus)}
                </span>
                {item.isHolding ? (
                  <span><BriefcaseBusiness aria-hidden="true" />持仓</span>
                ) : <span>未持仓</span>}
              </div>
              <div className="monitoring-config">
                <span>{item.scheduleName ?? "未设置调度"}</span>
                <span>{translated(targetModeLabels, item.targetMode)}</span>
                <small>{translated(targetLabels, item.targetStatus)}</small>
              </div>
              <div className="monitoring-price">
                <strong>{item.lastPrice ? `¥ ${item.lastPrice}` : "暂无价格"}</strong>
                <span>{translated(zoneLabels, item.zone)}</span>
                <time dateTime={item.lastPriceAt ?? undefined}>
                  {formatShanghaiTime(item.lastPriceAt)}
                </time>
              </div>
              {item.warningCodes.length > 0 ? (
                <TriangleAlert
                  className="monitoring-row__warning"
                  aria-label="该股票部分数据暂不可用"
                />
              ) : null}
            </article>
          ))}
        </section>
      )}
    </main>
  )
}
