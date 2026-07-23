import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  XAxis,
  YAxis,
} from "recharts"

import type { DailyPriceBar } from "@/features/market-data/types"
import {
  ChartContainer,
  ChartTooltip,
  type ChartConfig,
} from "@/shared/ui/chart"

interface DailyPriceChartProps {
  items: DailyPriceBar[]
  support: number
  resistance: number
}

interface ChartPoint extends DailyPriceBar {
  openValue: number
  closeValue: number
}

const MAX_RENDERED_POINTS = 1_500

const chartConfig = {
  openValue: {
    label: "开盘价",
    color: "var(--chart-2)",
  },
  closeValue: {
    label: "收盘价",
    color: "var(--chart-1)",
  },
} satisfies ChartConfig

function amountText(value: string) {
  const amount = Number(value)
  if (!Number.isFinite(amount)) return value
  if (amount >= 100_000_000) return `${(amount / 100_000_000).toFixed(2)} 亿`
  if (amount >= 10_000) return `${(amount / 10_000).toFixed(2)} 万`
  return amount.toLocaleString("zh-CN")
}

function PriceTooltip({
  active,
  payload,
}: {
  active?: boolean
  payload?: ReadonlyArray<{ payload?: ChartPoint }>
}) {
  const point = payload?.[0]?.payload
  if (!active || !point) return null

  return (
    <div className="grid min-w-48 gap-1.5 border bg-background p-3 text-xs shadow-lg">
      <p className="font-medium">{point.tradeDate}</p>
      <dl className="grid grid-cols-2 gap-x-5 gap-y-1">
        <dt className="text-muted-foreground">开盘价</dt>
        <dd className="text-right font-mono">{point.open}</dd>
        <dt className="text-muted-foreground">收盘价</dt>
        <dd className="text-right font-mono">{point.close}</dd>
        <dt className="text-muted-foreground">最高价</dt>
        <dd className="text-right font-mono">{point.high}</dd>
        <dt className="text-muted-foreground">最低价</dt>
        <dd className="text-right font-mono">{point.low}</dd>
        <dt className="text-muted-foreground">成交量</dt>
        <dd className="text-right font-mono">
          {point.volume.toLocaleString("zh-CN")}
        </dd>
        <dt className="text-muted-foreground">成交额</dt>
        <dd className="text-right font-mono">{amountText(point.amount)}</dd>
      </dl>
    </div>
  )
}

export function DailyPriceChart({
  items,
  support,
  resistance,
}: DailyPriceChartProps) {
  const step = Math.max(1, Math.ceil(items.length / MAX_RENDERED_POINTS))
  const renderedItems = items.filter(
    (_item, index) => index === 0 || index === items.length - 1 || index % step === 0,
  )
  const points: ChartPoint[] = renderedItems.map((item) => ({
    ...item,
    openValue: Number(item.open),
    closeValue: Number(item.close),
  }))

  return (
    <ChartContainer
      config={chartConfig}
      className="h-[420px] w-full aspect-auto"
      aria-label="股票开盘价和收盘价日线图"
    >
      <LineChart
        accessibilityLayer
        data={points}
        margin={{ top: 24, right: 30, bottom: 12, left: 8 }}
      >
        <CartesianGrid vertical={false} />
        <XAxis
          dataKey="tradeDate"
          minTickGap={32}
          tickLine={false}
          axisLine={false}
          tickFormatter={(value: string) => value.slice(2)}
        />
        <YAxis
          domain={["auto", "auto"]}
          width={68}
          tickLine={false}
          axisLine={false}
          tickFormatter={(value: number) => value.toFixed(2)}
        />
        <ChartTooltip
          cursor={{ stroke: "var(--border)" }}
          content={<PriceTooltip />}
        />
        <ReferenceLine
          y={support}
          stroke="var(--chart-3)"
          strokeWidth={2}
          label={{
            value: `支撑位 ${support.toFixed(2)}`,
            position: "insideBottomLeft",
            fill: "var(--foreground)",
          }}
        />
        <ReferenceLine
          y={resistance}
          stroke="var(--chart-4)"
          strokeDasharray="7 5"
          strokeWidth={2}
          label={{
            value: `压力位 ${resistance.toFixed(2)}`,
            position: "insideTopLeft",
            fill: "var(--foreground)",
          }}
        />
        <Line
          dataKey="openValue"
          name="开盘价"
          type="monotone"
          stroke="var(--color-openValue)"
          strokeWidth={1.5}
          dot={false}
          isAnimationActive={false}
        />
        <Line
          dataKey="closeValue"
          name="收盘价"
          type="monotone"
          stroke="var(--color-closeValue)"
          strokeWidth={2}
          dot={false}
          isAnimationActive={false}
        />
      </LineChart>
    </ChartContainer>
  )
}
