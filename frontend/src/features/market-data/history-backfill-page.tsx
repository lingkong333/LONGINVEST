import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { RotateCcwIcon } from "lucide-react"
import { useState } from "react"
import { useParams, useSearchParams } from "react-router-dom"

import { marketDataGateway } from "@/features/market-data/gateway"
import type {
  BackfillItemStatus,
  HistoryProviderCode,
  MarketDataGateway,
} from "@/features/market-data/types"
import { Badge } from "@/shared/ui/badge"
import { Button } from "@/shared/ui/button"
import { Checkbox } from "@/shared/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/shared/ui/dialog"
import { Input } from "@/shared/ui/input"
import { Label } from "@/shared/ui/label"
import { PageState } from "@/shared/ui/page-state"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/select"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/shared/ui/table"

const PAGE_SIZE = 100
const statuses: { value: BackfillItemStatus | "ALL"; label: string }[] = [
  { value: "ALL", label: "全部" },
  { value: "PENDING", label: "待处理" },
  { value: "RUNNING", label: "处理中" },
  { value: "SUCCEEDED", label: "已完成" },
  { value: "ANOMALY", label: "异常" },
  { value: "FAILED", label: "失败" },
  { value: "CANCELED", label: "已停止" },
]

type HistoryBackfillGateway = MarketDataGateway & Required<Pick<
  MarketDataGateway,
  "loadBackfillItems" | "retryBackfillItems"
>>

export function HistoryBackfillPage({
  gateway = marketDataGateway as HistoryBackfillGateway,
}: { gateway?: HistoryBackfillGateway }) {
  const { jobId = "" } = useParams<{ jobId: string }>()
  const [params, setParams] = useSearchParams()
  const status = (params.get("status") || "ALL") as BackfillItemStatus | "ALL"
  const page = Math.max(1, Number(params.get("page")) || 1)
  const [selected, setSelected] = useState<string[]>([])
  const [retrying, setRetrying] = useState<string[] | null>(null)
  const [provider, setProvider] = useState<HistoryProviderCode | "AUTO">("AUTO")
  const [concurrency, setConcurrency] = useState("4")
  const queryClient = useQueryClient()
  const items = useQuery({
    queryKey: ["market-data", "backfill-items", jobId, status, page],
    queryFn: () => gateway.loadBackfillItems({
      jobId,
      status: status === "ALL" ? undefined : status,
      page,
      pageSize: PAGE_SIZE,
    }),
    enabled: Boolean(jobId),
    refetchInterval: 3_000,
  })
  const retry = useMutation({
    mutationFn: (symbols: string[]) => gateway.retryBackfillItems({
      jobId,
      symbols,
      providerCode: provider === "AUTO" ? undefined : provider,
      concurrency: Number(concurrency),
      reason: "管理员从历史补全明细重试",
    }),
    onSuccess: async () => {
      setRetrying(null)
      setSelected([])
      await queryClient.invalidateQueries({ queryKey: ["market-data", "backfills"] })
    },
  })
  const retryable = items.data?.items.filter(
    (item) => item.status === "FAILED" || item.status === "ANOMALY",
  ) ?? []
  const allSelected = retryable.length > 0
    && retryable.every((item) => selected.includes(item.symbol))

  return (
    <main className="mx-auto w-full max-w-[1500px] px-4 py-5 sm:px-6">
      <header className="mb-5 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">历史补全明细</h1>
          <p className="mt-1 font-mono text-sm text-muted-foreground">{jobId}</p>
        </div>
        <Button
          disabled={selected.length === 0}
          onClick={() => setRetrying(selected)}
        >
          <RotateCcwIcon data-icon="inline-start" />
          批量重试 ({selected.length})
        </Button>
      </header>

      <div className="mb-4 w-48">
        <Label className="mb-2">处理状态</Label>
        <Select
          value={status}
          onValueChange={(value) => setParams({ status: value, page: "1" })}
        >
          <SelectTrigger><SelectValue /></SelectTrigger>
          <SelectContent>
            {statuses.map((item) => (
              <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {items.isPending ? (
        <PageState state="loading" title="正在读取逐股结果" description="正在加载任务检查点。" />
      ) : items.isError ? (
        <PageState state="error" title="逐股结果暂时无法读取" description="请稍后重试。" />
      ) : items.data.items.length === 0 ? (
        <PageState state="empty" title="当前筛选没有股票" description="可切换其他处理状态。" />
      ) : (
        <>
        <div className="overflow-x-auto border">
          <Table className="min-w-[820px]">
            <TableHeader>
              <TableRow>
                <TableHead className="w-12">
                  <Checkbox
                    checked={allSelected}
                    aria-label="选择本页全部异常和失败股票"
                    onCheckedChange={(checked) => setSelected(
                      checked ? retryable.map((item) => item.symbol) : [],
                    )}
                  />
                </TableHead>
                <TableHead>股票</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>异常说明</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.data.items.map((item) => {
                const canRetry = item.status === "FAILED" || item.status === "ANOMALY"
                return (
                  <TableRow key={item.securityId}>
                    <TableCell>
                      {canRetry ? (
                        <Checkbox
                          checked={selected.includes(item.symbol)}
                          aria-label={`选择 ${item.symbol}`}
                          onCheckedChange={(checked) => setSelected((current) => (
                            checked
                              ? [...new Set([...current, item.symbol])]
                              : current.filter((symbol) => symbol !== item.symbol)
                          ))}
                        />
                      ) : null}
                    </TableCell>
                    <TableCell className="font-mono font-medium">{item.symbol}</TableCell>
                    <TableCell><Badge variant="outline">{statusLabel(item.status)}</Badge></TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {item.errorCode ?? (
                        item.anomalyRows.length > 0
                          ? `${item.anomalyRows.length} 个异常交易日已跳过`
                          : "-"
                      )}
                    </TableCell>
                    <TableCell className="text-right">
                      {canRetry ? (
                        <Button size="sm" variant="outline" onClick={() => setRetrying([item.symbol])}>
                          <RotateCcwIcon data-icon="inline-start" />重试
                        </Button>
                      ) : null}
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        </div>
        <div className="mt-3 flex items-center justify-end gap-2">
          <span className="text-sm text-muted-foreground">
            共 {items.data.pagination.total} 只
          </span>
          <Button
            size="sm"
            variant="outline"
            disabled={page <= 1}
            onClick={() => setParams({ status, page: String(page - 1) })}
          >上一页</Button>
          <Button
            size="sm"
            variant="outline"
            disabled={page * PAGE_SIZE >= items.data.pagination.total}
            onClick={() => setParams({ status, page: String(page + 1) })}
          >下一页</Button>
        </div>
        </>
      )}

      <Dialog open={retrying !== null} onOpenChange={(open) => !open && setRetrying(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>重试所选股票</DialogTitle>
            <DialogDescription>
              将创建一个只包含 {retrying?.length ?? 0} 只股票的新任务。
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 py-2">
            <div className="grid gap-2">
              <Label htmlFor="retry-provider">数据源</Label>
              <Select
                value={provider}
                onValueChange={(value) => setProvider(
                  value as HistoryProviderCode | "AUTO",
                )}
              >
                <SelectTrigger id="retry-provider"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="AUTO">自动路由</SelectItem>
                  <SelectItem value="SINA">新浪</SelectItem>
                  <SelectItem value="EASTMONEY">东方财富</SelectItem>
                  <SelectItem value="BAOSTOCK">BaoStock</SelectItem>
                  <SelectItem value="TUSHARE">Tushare</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="retry-concurrency">并发数</Label>
              <Input id="retry-concurrency" type="number" min="1" value={concurrency} onChange={(event) => setConcurrency(event.target.value)} />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRetrying(null)}>取消</Button>
            <Button
              disabled={retry.isPending || !Number.isInteger(Number(concurrency)) || Number(concurrency) < 1}
              onClick={() => retrying && retry.mutate(retrying)}
            >确认重试</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </main>
  )
}

function statusLabel(status: BackfillItemStatus) {
  return {
    PENDING: "待处理",
    RUNNING: "处理中",
    SUCCEEDED: "已完成",
    ANOMALY: "异常",
    FAILED: "失败",
    CANCELED: "已停止",
  }[status]
}
