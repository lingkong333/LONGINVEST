import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  BriefcaseBusiness,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  History,
  Search,
  ShieldCheck,
  UsersRound,
} from "lucide-react"
import { useEffect, useMemo, useState } from "react"

import { useAuth } from "@/features/auth"
import { positionGateway } from "@/features/positions/gateway"
import type {
  PositionAction,
  PositionGateway,
  PositionItem,
} from "@/features/positions/types"
import { ApiError } from "@/shared/api/client"
import { Alert, AlertDescription } from "@/shared/ui/alert"
import { Badge } from "@/shared/ui/badge"
import { Button } from "@/shared/ui/button"
import { Card, CardContent } from "@/shared/ui/card"
import { Checkbox } from "@/shared/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogTitle,
} from "@/shared/ui/dialog"
import { Input } from "@/shared/ui/input"
import { PageState } from "@/shared/ui/page-state"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/shared/ui/table"
import { Tabs, TabsList, TabsTrigger } from "@/shared/ui/tabs"
import { Textarea } from "@/shared/ui/textarea"

const PAGE_SIZE = 12

const statusLabel = {
  HOLDING: "已持仓",
  NOT_HOLDING: "未持仓",
} as const

const actionCopy = {
  HOLD: {
    title: "标记为已持仓",
    description: "系统只记录持仓状态，不记录数量、成本、成交或盈亏。",
  },
  CLEAR: {
    title: "标记为未持仓",
    description: "清仓状态立即生效，尚未发送的高位提醒可能被取消。",
  },
} as const

type PendingChange = {
  action: PositionAction
  items: PositionItem[]
}

function formatShanghaiTime(value: string | null) {
  if (!value) {
    return "暂无"
  }
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value))
}

function sourceLabel(source: string) {
  return source === "manual" ? "手工修改" : source
}

export function PositionsPage({
  gateway = positionGateway,
}: {
  gateway?: PositionGateway
}) {
  const { invalidate } = useAuth()
  const queryClient = useQueryClient()
  const [activeTab, setActiveTab] = useState<"current" | "history">("current")
  const [search, setSearch] = useState("")
  const [selectedSymbols, setSelectedSymbols] = useState<Set<string>>(
    () => new Set(),
  )
  const [pendingChange, setPendingChange] = useState<PendingChange | null>(null)
  const [reason, setReason] = useState("")
  const [note, setNote] = useState("")
  const [historyPage, setHistoryPage] = useState(1)
  const [successMessage, setSuccessMessage] = useState("")

  const currentQuery = useQuery({
    queryKey: ["positions", "current"],
    queryFn: gateway.loadCurrent,
  })
  const historyQuery = useQuery({
    queryKey: ["positions", "history"],
    queryFn: gateway.loadHistory,
    enabled: activeTab === "history",
  })
  const changeMutation = useMutation({
    mutationFn: async () => {
      if (!pendingChange) {
        return []
      }
      if (pendingChange.items.length === 1) {
        const [item] = pendingChange.items
        await gateway.changePosition({
          symbol: item.symbol,
          action: pendingChange.action,
          expectedVersion: item.version,
          reason: reason.trim(),
          note: note.trim() || null,
        })
        return []
      }
      return gateway.changeBatch({
        items: pendingChange.items.map((item) => ({
          symbol: item.symbol,
          action: pendingChange.action,
          expectedVersion: item.version,
        })),
        reason: reason.trim(),
        note: note.trim() || null,
      })
    },
    onSuccess: async (results) => {
      const failed = results.filter((result) => (
        result.status === "FAILED" || result.status === "REJECTED"
      ))
      setSuccessMessage(
        failed.length > 0
          ? `批量操作已完成，${results.length - failed.length} 只成功，${failed.length} 只失败。`
          : pendingChange && pendingChange.items.length > 1
            ? `已更新 ${pendingChange.items.length} 只股票的持仓状态。`
            : "持仓状态已更新。",
      )
      closeDialog()
      setSelectedSymbols(new Set())
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["positions", "current"] }),
        queryClient.invalidateQueries({ queryKey: ["positions", "history"] }),
      ])
    },
  })

  useEffect(() => {
    const errors = [currentQuery.error, historyQuery.error, changeMutation.error]
    if (errors.some((error) => (
      error instanceof ApiError && error.status === 401
    ))) {
      invalidate()
    }
  }, [changeMutation.error, currentQuery.error, historyQuery.error, invalidate])

  const currentItems = useMemo(
    () => currentQuery.data?.items ?? [],
    [currentQuery.data?.items],
  )
  const visibleItems = useMemo(() => {
    const keyword = search.trim().toLocaleLowerCase("zh-CN")
    if (!keyword) {
      return currentItems
    }
    return currentItems.filter((item) => (
      item.symbol.toLocaleLowerCase("zh-CN").includes(keyword)
      || (item.securityName ?? "").toLocaleLowerCase("zh-CN").includes(keyword)
    ))
  }, [currentItems, search])
  const selectedItems = useMemo(
    () => currentItems.filter((item) => selectedSymbols.has(item.symbol)),
    [currentItems, selectedSymbols],
  )
  const historyItems = historyQuery.data ?? []
  const totalHistoryPages = Math.max(1, Math.ceil(historyItems.length / PAGE_SIZE))
  const visibleHistory = historyItems.slice(
    (historyPage - 1) * PAGE_SIZE,
    historyPage * PAGE_SIZE,
  )

  function closeDialog() {
    setPendingChange(null)
    setReason("")
    setNote("")
    changeMutation.reset()
  }

  function beginChange(action: PositionAction, items: PositionItem[]) {
    setSuccessMessage("")
    setPendingChange({ action, items })
    setReason("")
    setNote("")
    changeMutation.reset()
  }

  function canBatch(action: PositionAction) {
    return (
      selectedItems.length > 0
      && selectedItems.every((item) => item.allowedActions.includes(action))
    )
  }

  return (
    <main className="mx-auto flex w-full max-w-[1500px] flex-col gap-6 px-4 py-6 lg:px-8">
      <header className="grid gap-5 border-b pb-5 lg:grid-cols-[1fr_auto] lg:items-end">
        <div>
          <div className="mb-3 flex items-center gap-2 text-sm font-medium text-muted-foreground">
            <ShieldCheck className="size-4" aria-hidden="true" />
            状态记录，不是交易账户
          </div>
          <h1 className="text-3xl font-semibold">持仓管理</h1>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-muted-foreground">
            这里只记录股票是否持有，不保存数量、成本、成交记录和真实盈亏。
            持仓变化会保留独立历史，且不能回填过去的生效时间。
          </p>
        </div>
        <Tabs value={activeTab} onValueChange={(value) => setActiveTab(value as "current" | "history")}>
          <TabsList aria-label="持仓视图">
            <TabsTrigger value="current">
              <BriefcaseBusiness aria-hidden="true" />当前状态
            </TabsTrigger>
            <TabsTrigger value="history">
              <History aria-hidden="true" />修改历史
            </TabsTrigger>
          </TabsList>
        </Tabs>
      </header>

      {successMessage ? (
        <Alert>
          <CheckCircle2 aria-hidden="true" />
          <AlertDescription>{successMessage}</AlertDescription>
        </Alert>
      ) : null}

      {activeTab === "current" ? (
        <section className="grid gap-4" aria-label="当前持仓状态">
          <Card>
            <CardContent className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
            <label className="relative block w-full md:max-w-sm">
              <Search className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
              <span className="sr-only">搜索股票代码或名称</span>
              <Input
                className="pl-9"
                value={search}
                placeholder="搜索股票代码或名称"
                onChange={(event) => setSearch(event.target.value)}
              />
            </label>
            <div className="flex flex-wrap items-center gap-2">
              <span className="mr-1 text-sm text-muted-foreground">
                已选择 {selectedItems.length} 只
              </span>
              <Button
                variant="outline"
                disabled={!canBatch("HOLD")}
                onClick={() => beginChange("HOLD", selectedItems)}
              >
                批量标记持仓
              </Button>
              <Button
                variant="outline"
                disabled={!canBatch("CLEAR")}
                onClick={() => beginChange("CLEAR", selectedItems)}
              >
                批量标记清仓
              </Button>
            </div>
            </CardContent>
          </Card>

          {currentQuery.isPending ? (
            <PageState
              state="loading"
              title="正在读取持仓"
              description="正在核对最新持仓状态和监控关系。"
            />
          ) : currentQuery.isError ? (
            <PageState
              state="error"
              title="持仓状态暂时无法读取"
              description="请稍后重新加载。已保存的持仓状态不会受到影响。"
              action={{
                label: "重新加载持仓",
                onClick: () => void currentQuery.refetch(),
              }}
              error={currentQuery.error instanceof ApiError ? {
                code: currentQuery.error.code,
                requestId: currentQuery.error.requestId,
              } : { code: "POSITION_LIST_FAILED" }}
            />
          ) : currentItems.length === 0 ? (
            <PageState
              state="empty"
              title="还没有持仓状态记录"
              description="首次标记某只股票为持仓后，会在这里显示当前状态。"
            />
          ) : (
            <>
              {currentQuery.data.warningCodes.length > 0 ? (
                <Alert>
                  <CircleAlert aria-hidden="true" />
                  <AlertDescription>监控关系暂时无法核对，持仓状态仍可正常查看。</AlertDescription>
                </Alert>
              ) : null}
              {visibleItems.length === 0 ? (
                <PageState
                  state="empty"
                  title="没有符合条件的股票"
                  description="请调整股票代码或名称后重试。"
                />
              ) : (
                <Card className="overflow-hidden py-0">
                  <CardContent className="p-0">
                    <Table className="min-w-[940px]">
                      <TableHeader>
                        <TableRow>
                          <TableHead className="w-12 px-4">
                            <span className="sr-only">选择</span>
                          </TableHead>
                          <TableHead className="px-4">股票</TableHead>
                          <TableHead className="px-4">当前状态</TableHead>
                          <TableHead className="px-4">监控关系</TableHead>
                          <TableHead className="px-4">最近修改</TableHead>
                          <TableHead className="px-4">版本</TableHead>
                          <TableHead className="px-4 text-right">操作</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {visibleItems.map((item) => (
                          <TableRow key={item.securityId}>
                            <TableCell className="px-4 py-4">
                              <Checkbox
                                aria-label={`选择 ${item.symbol}`}
                                checked={selectedSymbols.has(item.symbol)}
                                onCheckedChange={(checked) => {
                                  setSelectedSymbols((current) => {
                                    const next = new Set(current)
                                    if (checked === true) {
                                      next.add(item.symbol)
                                    } else {
                                      next.delete(item.symbol)
                                    }
                                    return next
                                  })
                                }}
                              />
                            </TableCell>
                            <TableCell className="px-4 py-4">
                              <strong className="block font-semibold">
                                {item.securityName ?? item.symbol}
                              </strong>
                              <span className="mt-1 block font-mono text-xs text-muted-foreground">
                                {item.symbol}
                              </span>
                            </TableCell>
                            <TableCell className="px-4 py-4">
                              <Badge variant={item.status === "HOLDING" ? "default" : "secondary"}>
                                {statusLabel[item.status]}
                              </Badge>
                            </TableCell>
                            <TableCell className="px-4 py-4">
                              {item.isMonitored === true ? (
                                <span className="text-muted-foreground">已加入监控</span>
                              ) : item.isMonitored === false && item.status === "HOLDING" ? (
                                <div>
                                  <span className="block font-medium text-destructive">已持仓 · 未监控</span>
                                  <a
                                    className="mt-1 inline-block text-xs font-medium text-primary underline-offset-4 hover:underline"
                                    href={`/monitoring?symbol=${encodeURIComponent(item.symbol)}`}
                                  >
                                    前往加入监控
                                  </a>
                                </div>
                              ) : item.isMonitored === null ? (
                                <span className="text-muted-foreground">暂时无法核对</span>
                              ) : (
                                <span className="text-muted-foreground">未监控</span>
                              )}
                            </TableCell>
                            <TableCell className="px-4 py-4">
                              <span className="block">{formatShanghaiTime(item.updatedAt)}</span>
                              <span className="mt-1 block text-xs text-muted-foreground">
                                {item.source ? sourceLabel(item.source) : "暂无来源"}
                              </span>
                            </TableCell>
                            <TableCell className="px-4 py-4 font-mono">v{item.version}</TableCell>
                            <TableCell className="px-4 py-4 text-right">
                              <div className="flex justify-end gap-2">
                                {item.allowedActions.includes("HOLD") ? (
                                  <Button size="sm" onClick={() => beginChange("HOLD", [item])}>
                                    标记持仓
                                  </Button>
                                ) : null}
                                {item.allowedActions.includes("CLEAR") ? (
                                  <Button
                                    size="sm"
                                    variant="outline"
                                    onClick={() => beginChange("CLEAR", [item])}
                                  >
                                    标记清仓
                                  </Button>
                                ) : null}
                              </div>
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </CardContent>
                </Card>
              )}
            </>
          )}
        </section>
      ) : (
        <section className="grid gap-4" aria-label="持仓修改历史">
          {historyQuery.isPending ? (
            <PageState
              state="loading"
              title="正在读取修改历史"
              description="正在加载不可变的持仓状态记录。"
            />
          ) : historyQuery.isError ? (
            <PageState
              state="error"
              title="修改历史暂时无法读取"
              description="请稍后重新加载，当前持仓状态不会受到影响。"
              action={{
                label: "重新加载历史",
                onClick: () => void historyQuery.refetch(),
              }}
              error={historyQuery.error instanceof ApiError ? {
                code: historyQuery.error.code,
                requestId: historyQuery.error.requestId,
              } : { code: "POSITION_HISTORY_FAILED" }}
            />
          ) : historyItems.length === 0 ? (
            <PageState
              state="empty"
              title="还没有修改历史"
              description="持仓状态发生真实变化后，记录会显示在这里。"
            />
          ) : (
            <Card className="overflow-hidden py-0">
              <CardContent className="p-0">
                <Table className="min-w-[900px]">
                  <TableHeader>
                    <TableRow>
                      <TableHead className="px-4">生效时间</TableHead>
                      <TableHead className="px-4">股票</TableHead>
                      <TableHead className="px-4">状态变化</TableHead>
                      <TableHead className="px-4">版本</TableHead>
                      <TableHead className="px-4">备注</TableHead>
                      <TableHead className="px-4">来源</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {visibleHistory.map((item) => (
                      <TableRow key={item.id}>
                        <TableCell className="px-4 py-4">{formatShanghaiTime(item.effectiveAt)}</TableCell>
                        <TableCell className="px-4 py-4 font-mono">{item.symbol}</TableCell>
                        <TableCell className="px-4 py-4">
                          {item.beforeStatus ? statusLabel[item.beforeStatus] : "首次记录"}
                          <span className="mx-2 text-muted-foreground">→</span>
                          <strong>{statusLabel[item.afterStatus]}</strong>
                        </TableCell>
                        <TableCell className="px-4 py-4 font-mono">v{item.version}</TableCell>
                        <TableCell className="max-w-xs whitespace-normal px-4 py-4">
                          {item.note || "无备注"}
                        </TableCell>
                        <TableCell className="px-4 py-4">{sourceLabel(item.source)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </CardContent>
              <footer className="flex items-center justify-between border-t px-4 py-3">
                <span className="text-sm text-muted-foreground">
                  共 {historyItems.length} 条，第 {historyPage} / {totalHistoryPages} 页
                </span>
                <div className="flex gap-2">
                  <Button
                    size="icon-sm"
                    variant="outline"
                    aria-label="上一页"
                    disabled={historyPage === 1}
                    onClick={() => setHistoryPage((page) => Math.max(1, page - 1))}
                  >
                    <ChevronLeft aria-hidden="true" />
                  </Button>
                  <Button
                    size="icon-sm"
                    variant="outline"
                    aria-label="下一页"
                    disabled={historyPage === totalHistoryPages}
                    onClick={() => setHistoryPage((page) => (
                      Math.min(totalHistoryPages, page + 1)
                    ))}
                  >
                    <ChevronRight aria-hidden="true" />
                  </Button>
                </div>
              </footer>
            </Card>
          )}
        </section>
      )}

      <Dialog open={pendingChange !== null} onOpenChange={(open) => {
        if (!open && !changeMutation.isPending) {
          closeDialog()
        }
      }}>
        <DialogContent>
          <DialogTitle>
            {pendingChange
              ? `${actionCopy[pendingChange.action].title}${pendingChange.items.length > 1 ? `（${pendingChange.items.length} 只）` : ""}`
              : "确认持仓修改"}
          </DialogTitle>
          <DialogDescription>
            {pendingChange ? actionCopy[pendingChange.action].description : ""}
          </DialogDescription>
          {pendingChange && pendingChange.items.length > 1 ? (
            <div className="flex items-start gap-2 rounded-lg bg-muted/60 p-3 text-sm">
              <UsersRound className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
              批量操作会逐只处理。单只失败不会撤销其他已成功的修改。
            </div>
          ) : null}
          <label className="grid gap-2 text-sm font-medium">
            操作原因
            <Input
              value={reason}
              maxLength={500}
              placeholder="请说明本次修改原因"
              aria-invalid={reason.length > 0 && !reason.trim()}
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
          <label className="grid gap-2 text-sm font-medium">
            备注（可选）
            <Textarea
              className="min-h-24 resize-y"
              value={note}
              maxLength={500}
              placeholder="可补充持仓判断依据，不要填写数量或成本"
              onChange={(event) => setNote(event.target.value)}
            />
          </label>
          {changeMutation.isError ? (
            <Alert variant="destructive">
              <AlertDescription>
              {changeMutation.error instanceof ApiError
                && changeMutation.error.code === "POSITION_VERSION_CONFLICT"
                ? "持仓状态已被其他操作修改。请关闭窗口并重新加载后再试。"
                : "本次持仓修改没有完成，请检查原因后重试。"}
              {changeMutation.error instanceof ApiError ? (
                <span className="mt-1 block text-xs text-muted-foreground">
                  错误码：{changeMutation.error.code}
                </span>
              ) : null}
              </AlertDescription>
            </Alert>
          ) : null}
          <DialogFooter>
            <Button
              variant="outline"
              disabled={changeMutation.isPending}
              onClick={closeDialog}
            >
              取消
            </Button>
            <Button
              disabled={!reason.trim() || changeMutation.isPending}
              onClick={() => changeMutation.mutate()}
            >
              {changeMutation.isPending ? "正在提交" : "确认修改"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </main>
  )
}
