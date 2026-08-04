import { useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import {
  ArrowLeftIcon,
  CalendarRangeIcon,
  MinusIcon,
  PlusIcon,
} from "lucide-react"
import { Link, useParams } from "react-router-dom"

import { DailyPriceChart } from "@/features/market-data/daily-price-chart"
import { marketDataGateway } from "@/features/market-data/gateway"
import type {
  DailyPriceMode,
  MarketDataGateway,
} from "@/features/market-data/types"
import { ApiError } from "@/shared/api/client"
import { Alert, AlertDescription, AlertTitle } from "@/shared/ui/alert"
import { Badge } from "@/shared/ui/badge"
import { Button } from "@/shared/ui/button"
import { Input } from "@/shared/ui/input"
import { Label } from "@/shared/ui/label"
import { PageState } from "@/shared/ui/page-state"
import {
  ToggleGroup,
  ToggleGroupItem,
} from "@/shared/ui/toggle-group"

interface SecurityDetailPageProps {
  gateway?: MarketDataGateway
  symbol?: string
}

interface DateRange {
  startDate: string
  endDate: string
}

const rangeOptions = [
  { value: "1M", label: "1 月", months: 1 },
  { value: "3M", label: "3 月", months: 3 },
  { value: "6M", label: "6 月", months: 6 },
  { value: "1Y", label: "1 年", months: 12 },
  { value: "3Y", label: "3 年", months: 36 },
  { value: "ALL", label: "全部", months: null },
] as const

function shanghaiToday() {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date())
}

function subtractMonths(value: string, months: number) {
  const [year, month, day] = value.split("-").map(Number)
  const date = new Date(Date.UTC(year, month - 1, 1))
  date.setUTCMonth(date.getUTCMonth() - months)
  const lastDay = new Date(Date.UTC(
    date.getUTCFullYear(),
    date.getUTCMonth() + 1,
    0,
  )).getUTCDate()
  date.setUTCDate(Math.min(day, lastDay))
  return date.toISOString().slice(0, 10)
}

function presetRange(value: string): DateRange {
  const endDate = shanghaiToday()
  const option = rangeOptions.find((item) => item.value === value)
  return {
    startDate: option?.months === null
      ? "1990-01-01"
      : subtractMonths(endDate, option?.months ?? 12),
    endDate,
  }
}

function decimalValue(value: string) {
  if (!/^\d+(?:\.\d{1,2})?$/.test(value.trim())) return null
  const number = Number(value)
  return Number.isFinite(number) && number > 0 ? number : null
}

function adjustedPrice(value: string, step: number) {
  const current = decimalValue(value) ?? 0
  return Math.max(0.01, Math.round((current + step) * 100) / 100).toFixed(2)
}

function errorDetails(error: Error) {
  return error instanceof ApiError
    ? { code: error.code, requestId: error.requestId }
    : { code: "UNKNOWN_ERROR" }
}

export function SecurityDetailPage({
  gateway = marketDataGateway,
  symbol: symbolProperty,
}: SecurityDetailPageProps) {
  const route = useParams<{ symbol: string }>()
  const symbol = (symbolProperty ?? route.symbol ?? "").trim().toUpperCase()
  const initialRange = useMemo(() => presetRange("ALL"), [])
  const [mode, setMode] = useState<DailyPriceMode>("UNADJUSTED")
  const [selectedRange, setSelectedRange] = useState("ALL")
  const [rangeDraft, setRangeDraft] = useState(initialRange)
  const [range, setRange] = useState(initialRange)
  const [rangeError, setRangeError] = useState("")
  const [supportInput, setSupportInput] = useState<string | null>(null)
  const [resistanceInput, setResistanceInput] = useState<string | null>(null)

  const security = useQuery({
    queryKey: ["market-data", "stock-detail", symbol],
    queryFn: () => gateway.loadSecurity(symbol),
    enabled: symbol.length > 0,
  })
  const prices = useQuery({
    queryKey: [
      "market-data",
      "daily-prices",
      symbol,
      mode,
      range.startDate,
      range.endDate,
    ],
    queryFn: () => gateway.loadDailyPrices({
      symbol,
      mode,
      startDate: range.startDate,
      endDate: range.endDate,
    }),
    enabled: symbol.length > 0,
    retry: false,
  })

  const defaultLevels = useMemo(() => {
    if (!prices.data?.items.length) {
      return { support: "", resistance: "" }
    }
    const lows = prices.data.items.map((item) => Number(item.low))
    const highs = prices.data.items.map((item) => Number(item.high))
    return {
      support: Math.min(...lows).toFixed(2),
      resistance: Math.max(...highs).toFixed(2),
    }
  }, [prices.data])
  const displayedSupport = supportInput ?? defaultLevels.support
  const displayedResistance = resistanceInput ?? defaultLevels.resistance

  function chooseRange(value: string) {
    if (!value) return
    const next = presetRange(value)
    setSelectedRange(value)
    setRangeDraft(next)
    setRange(next)
    setRangeError("")
  }

  function applyCustomRange() {
    if (!rangeDraft.startDate || !rangeDraft.endDate) {
      setRangeError("请选择开始日期和结束日期。")
      return
    }
    if (rangeDraft.startDate > rangeDraft.endDate) {
      setRangeError("开始日期不能晚于结束日期。")
      return
    }
    setSelectedRange("")
    setRange(rangeDraft)
    setRangeError("")
  }

  const support = decimalValue(displayedSupport)
  const resistance = decimalValue(displayedResistance)
  const levelError = support === null || resistance === null
    ? "支撑位和压力位必须是大于 0 的价格，最多保留两位小数。"
    : support >= resistance
      ? "支撑位必须低于压力位。"
      : ""

  if (!symbol) {
    return (
      <PageState
        state="error"
        title="股票代码无效"
        description="请从股票列表重新进入详情。"
        error={{ code: "SECURITY_SYMBOL_INVALID" }}
      />
    )
  }

  if (security.isPending) {
    return (
      <PageState
        state="loading"
        title="正在读取股票资料"
        description={`正在加载 ${symbol}。`}
      />
    )
  }

  if (security.isError) {
    return (
      <PageState
        state="error"
        title="股票资料暂时无法读取"
        description="请确认股票代码，或稍后重新加载。"
        action={{
          label: "重新加载",
          onClick: () => void security.refetch(),
        }}
        error={errorDetails(security.error)}
      />
    )
  }

  return (
    <main className="mx-auto w-full max-w-[1500px] px-4 py-5 sm:px-6">
      <header className="mb-5 flex flex-wrap items-start justify-between gap-4">
        <div>
          <Button asChild variant="ghost" className="mb-2 px-0">
            <Link to="/stocks">
              <ArrowLeftIcon data-icon="inline-start" />
              返回股票列表
            </Link>
          </Button>
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-2xl font-semibold">{security.data.name}</h1>
            <span className="font-mono text-muted-foreground">{symbol}</span>
            {security.data.isSt ? (
              <Badge variant="destructive">ST</Badge>
            ) : null}
            {security.data.isSuspended ? (
              <Badge variant="outline">停牌</Badge>
            ) : null}
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            {security.data.market} · {security.data.securityType} ·
            {security.data.listedOn
              ? ` ${security.data.listedOn} 上市`
              : " 上市日期未知"}
          </p>
        </div>
        <ToggleGroup
          type="single"
          variant="outline"
          value={mode}
          onValueChange={(value) => {
            if (value) setMode(value as DailyPriceMode)
          }}
          aria-label="价格口径"
        >
          <ToggleGroupItem value="UNADJUSTED">不复权</ToggleGroupItem>
          <ToggleGroupItem value="QFQ">前复权</ToggleGroupItem>
        </ToggleGroup>
      </header>

      <section className="border-y py-4" aria-label="日期范围">
        <div className="flex flex-wrap items-end gap-3">
          <div>
            <Label className="mb-2">常用范围</Label>
            <ToggleGroup
              type="single"
              variant="outline"
              value={selectedRange}
              onValueChange={chooseRange}
              aria-label="常用日期范围"
              className="flex-wrap justify-start"
            >
              {rangeOptions.map((option) => (
                <ToggleGroupItem key={option.value} value={option.value}>
                  {option.label}
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
          </div>
          <div>
            <Label htmlFor="daily-start" className="mb-2">开始日期</Label>
            <Input
              id="daily-start"
              type="date"
              value={rangeDraft.startDate}
              onChange={(event) => setRangeDraft((current) => ({
                ...current,
                startDate: event.target.value,
              }))}
            />
          </div>
          <div>
            <Label htmlFor="daily-end" className="mb-2">结束日期</Label>
            <Input
              id="daily-end"
              type="date"
              value={rangeDraft.endDate}
              onChange={(event) => setRangeDraft((current) => ({
                ...current,
                endDate: event.target.value,
              }))}
            />
          </div>
          <Button type="button" onClick={applyCustomRange}>
            <CalendarRangeIcon data-icon="inline-start" />
            应用范围
          </Button>
        </div>
        {rangeError ? (
          <p role="alert" className="mt-2 text-sm text-destructive">
            {rangeError}
          </p>
        ) : null}
      </section>

      <section className="mt-5" aria-labelledby="price-chart-title">
        <div className="mb-4 flex flex-wrap items-end justify-between gap-4">
          <div>
            <h2 id="price-chart-title" className="text-lg font-semibold">
              日线价格
            </h2>
            <p className="mt-1 text-sm text-muted-foreground">
              {range.startDate} 至 {range.endDate} ·
              {mode === "UNADJUSTED" ? " 不复权" : " 前复权"}
            </p>
          </div>
          {prices.data ? (
            <Badge variant="outline">
              完整读取 {prices.data.total.toLocaleString("zh-CN")} 个交易日
            </Badge>
          ) : null}
        </div>

        <div className="mb-4 grid gap-3 sm:grid-cols-2 lg:max-w-2xl">
          <PriceLevelField
            label="支撑位"
            value={displayedSupport}
            lineStyle="实线"
            onChange={setSupportInput}
          />
          <PriceLevelField
            label="压力位"
            value={displayedResistance}
            lineStyle="虚线"
            onChange={setResistanceInput}
          />
        </div>
        <p className="mb-4 text-xs text-muted-foreground">
          两条线仅用于当前页面观察，不是系统目标，不会参与提醒或回测。
        </p>
        {displayedSupport && displayedResistance && levelError ? (
          <Alert variant="destructive" className="mb-4">
            <AlertTitle>观察线价格无效</AlertTitle>
            <AlertDescription>{levelError}</AlertDescription>
          </Alert>
        ) : null}

        {prices.isPending ? (
          <PageState
            state="loading"
            title="正在读取日线"
            description="长时间范围会自动读取全部分页，请稍候。"
          />
        ) : prices.isError ? (
          <PageState
            state="error"
            title={
              mode === "QFQ"
                ? "前复权日线暂时无法读取"
                : "不复权日线暂时无法读取"
            }
            description={
              mode === "QFQ"
                ? "该股票可能尚无当前有效前复权数据，可切回不复权查看。"
                : "请稍后重试，或调整日期范围。"
            }
            action={{
              label: "重新加载",
              onClick: () => void prices.refetch(),
            }}
            error={errorDetails(prices.error)}
          />
        ) : prices.data.items.length === 0 ? (
          <PageState
            state="empty"
            title="所选范围没有日线"
            description="请扩大日期范围，或确认股票在该时段已经上市。"
          />
        ) : support !== null && resistance !== null && !levelError ? (
          <>
            {prices.data.dataset?.freshness === "STALE" ? (
              <Alert className="mb-4">
                <AlertTitle>前复权数据已过期</AlertTitle>
                <AlertDescription>
                  当前仍展示旧的完整数据集，请以页面标注的数据日期为准。
                </AlertDescription>
              </Alert>
            ) : null}
            <DailyPriceChart
              items={prices.data.items}
              support={support}
              resistance={resistance}
              onSupportChange={(value) => setSupportInput(value.toFixed(2))}
              onResistanceChange={(value) => setResistanceInput(value.toFixed(2))}
            />
          </>
        ) : (
          <PageState
            state="empty"
            title="请设置有效的观察线"
            description="支撑位低于压力位后即可显示图表。"
          />
        )}
      </section>
    </main>
  )
}

function PriceLevelField({
  label,
  value,
  lineStyle,
  onChange,
}: {
  label: string
  value: string
  lineStyle: string
  onChange: (value: string) => void
}) {
  const id = `price-level-${label === "支撑位" ? "support" : "resistance"}`
  return (
    <div>
      <div className="mb-2 flex items-center justify-between gap-3">
        <Label htmlFor={id}>{label}</Label>
        <span className="text-xs text-muted-foreground">{lineStyle}</span>
      </div>
      <div className="flex">
        <Button
          type="button"
          size="icon"
          variant="outline"
          className="shrink-0 rounded-r-none"
          aria-label={`${label}减少 0.01 元`}
          title="减少 0.01 元"
          onClick={() => onChange(adjustedPrice(value, -0.01))}
        >
          <MinusIcon data-icon="icon" />
        </Button>
        <Input
          id={id}
          type="number"
          min="0.01"
          step="0.01"
          inputMode="decimal"
          className="rounded-none text-center font-mono"
          value={value}
          onChange={(event) => onChange(event.target.value)}
        />
        <Button
          type="button"
          size="icon"
          variant="outline"
          className="shrink-0 rounded-l-none"
          aria-label={`${label}增加 0.01 元`}
          title="增加 0.01 元"
          onClick={() => onChange(adjustedPrice(value, 0.01))}
        >
          <PlusIcon data-icon="icon" />
        </Button>
      </div>
    </div>
  )
}
