import { useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import {
  ArrowRightIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  SearchIcon,
  XIcon,
} from "lucide-react"
import { Link, useSearchParams } from "react-router-dom"

import { marketDataGateway } from "@/features/market-data/gateway"
import type { MarketDataGateway } from "@/features/market-data/types"
import { ApiError } from "@/shared/api/client"
import { Badge } from "@/shared/ui/badge"
import { Button } from "@/shared/ui/button"
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

const PAGE_SIZE = 50

interface SecurityListPageProps {
  gateway?: MarketDataGateway
}

function positiveInteger(value: string | null) {
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed > 0 ? parsed : 1
}

function errorDetails(error: Error) {
  return error instanceof ApiError
    ? { code: error.code, requestId: error.requestId }
    : { code: "UNKNOWN_ERROR" }
}

export function SecurityListPage({
  gateway = marketDataGateway,
}: SecurityListPageProps) {
  const [searchParams, setSearchParams] = useSearchParams()
  const filters = useMemo(() => ({
    query: searchParams.get("q")?.trim() || undefined,
    page: positiveInteger(searchParams.get("page")),
    pageSize: PAGE_SIZE,
  }), [searchParams])
  const [searchDraft, setSearchDraft] = useState(filters.query ?? "")

  const securities = useQuery({
    queryKey: ["market-data", "stock-list", filters],
    queryFn: () => gateway.loadSecurityList(filters),
  })

  function submitSearch() {
    const next = new URLSearchParams()
    const query = searchDraft.trim()
    if (query) next.set("q", query)
    next.set("page", "1")
    setSearchParams(next)
  }

  function clearSearch() {
    setSearchDraft("")
    setSearchParams({ page: "1" })
  }

  function changePage(page: number) {
    const next = new URLSearchParams(searchParams)
    next.set("page", String(page))
    setSearchParams(next)
  }

  const totalPages = securities.data
    ? Math.max(
        1,
        Math.ceil(
          securities.data.pagination.total
          / securities.data.pagination.pageSize,
        ),
      )
    : 1

  return (
    <main className="mx-auto w-full max-w-[1500px] px-4 py-5 sm:px-6">
      <header className="mb-5">
        <h1 className="text-2xl font-semibold">全部 A 股</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          上海、深圳和北京市场股票主数据
        </p>
      </header>

      <form
        className="mb-5 flex max-w-xl gap-2"
        role="search"
        onSubmit={(event) => {
          event.preventDefault()
          submitSearch()
        }}
      >
        <Input
          aria-label="搜索股票"
          placeholder="输入股票代码或名称"
          value={searchDraft}
          onChange={(event) => setSearchDraft(event.target.value)}
        />
        {filters.query ? (
          <Button
            type="button"
            size="icon"
            variant="outline"
            aria-label="清除搜索"
            title="清除搜索"
            onClick={clearSearch}
          >
            <XIcon data-icon="icon" />
          </Button>
        ) : null}
        <Button type="submit" disabled={securities.isFetching}>
          <SearchIcon data-icon="inline-start" />
          搜索
        </Button>
      </form>

      {securities.isPending ? (
        <PageState
          state="loading"
          title="正在读取股票列表"
          description="正在加载最新股票主数据。"
        />
      ) : securities.isError ? (
        <PageState
          state="error"
          title="股票列表暂时无法读取"
          description="请稍后重试，现有行情数据不会受到影响。"
          action={{
            label: "重新加载",
            onClick: () => void securities.refetch(),
          }}
          error={errorDetails(securities.error)}
        />
      ) : securities.data.items.length === 0 ? (
        <PageState
          state="empty"
          title={filters.query ? "没有匹配的股票" : "还没有股票主数据"}
          description={
            filters.query
              ? "请尝试其他股票代码或名称。"
              : "主数据刷新完成后，股票会显示在这里。"
          }
          action={filters.query
            ? { label: "清除搜索", onClick: clearSearch }
            : undefined}
        />
      ) : (
        <>
          <div className="overflow-x-auto border">
            <Table className="min-w-[760px]">
              <TableHeader>
                <TableRow>
                  <TableHead>代码</TableHead>
                  <TableHead>名称</TableHead>
                  <TableHead>市场</TableHead>
                  <TableHead>上市状态</TableHead>
                  <TableHead>特别状态</TableHead>
                  <TableHead className="text-right">日线详情</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {securities.data.items.map((security) => (
                  <TableRow key={security.id}>
                    <TableCell className="font-mono font-medium">
                      {security.symbol}
                    </TableCell>
                    <TableCell className="font-medium">
                      {security.name}
                    </TableCell>
                    <TableCell>{security.market}</TableCell>
                    <TableCell>
                      <Badge
                        variant={
                          security.listingStatus === "LISTED"
                            ? "default"
                            : "secondary"
                        }
                      >
                        {security.listingStatus === "LISTED"
                          ? "上市"
                          : security.listingStatus}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <div className="flex gap-1.5">
                        {security.isSt ? (
                          <Badge variant="destructive">ST</Badge>
                        ) : null}
                        {security.isSuspended ? (
                          <Badge variant="outline">停牌</Badge>
                        ) : null}
                        {!security.isSt && !security.isSuspended ? "—" : null}
                      </div>
                    </TableCell>
                    <TableCell className="text-right">
                      <Button asChild size="icon-sm" variant="ghost">
                        <Link
                          to={`/stocks/${encodeURIComponent(
                            security.symbol,
                          )}`}
                          aria-label={`查看 ${security.name} 日线详情`}
                          title="查看日线详情"
                        >
                          <ArrowRightIcon data-icon="icon" />
                        </Link>
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>

          <footer className="mt-4 flex flex-wrap items-center justify-between gap-3">
            <p className="text-sm text-muted-foreground">
              共 {securities.data.pagination.total.toLocaleString("zh-CN")} 只，
              第 {securities.data.pagination.page} / {totalPages} 页
            </p>
            <div className="flex gap-2">
              <Button
                type="button"
                size="icon"
                variant="outline"
                aria-label="上一页"
                title="上一页"
                disabled={filters.page <= 1 || securities.isFetching}
                onClick={() => changePage(filters.page - 1)}
              >
                <ChevronLeftIcon data-icon="icon" />
              </Button>
              <Button
                type="button"
                size="icon"
                variant="outline"
                aria-label="下一页"
                title="下一页"
                disabled={
                  filters.page >= totalPages || securities.isFetching
                }
                onClick={() => changePage(filters.page + 1)}
              >
                <ChevronRightIcon data-icon="icon" />
              </Button>
            </div>
          </footer>
        </>
      )}
    </main>
  )
}
