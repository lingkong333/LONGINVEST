import { z } from "zod"

import type {
  BackfillItemSummary,
  BackfillSummary,
  DailyBatchSummary,
  DailyPriceBar,
  DailyPriceMode,
  DailyPriceSeries,
  MarketDataGateway,
  QfqDatasetSummary,
  QualityIssueSummary,
  QuoteCheckResult,
  QuoteCycleSummary,
  QuoteItemSummary,
  SecuritySummary,
} from "@/features/market-data/types"
import {
  ApiError,
  createApiClient,
  createClientIdempotencyKey,
} from "@/shared/api/client"
import type { paths } from "@/shared/api/generated/schema"

const paginationSchema = z.object({
  page: z.number().int().positive(),
  page_size: z.number().int().positive(),
  total: z.number().int().nonnegative(),
})

const securitySchema = z.object({
    symbol: z.string().min(1),
    exchange_code: z.string().min(1),
    name: z.string().min(1),
    market: z.string().min(1),
    security_type: z.string().min(1),
    listed_on: z.string().nullable(),
    delisted_on: z.string().nullable(),
    listing_status: z.string().min(1),
    is_st: z.boolean(),
    is_suspended: z.boolean(),
    master_version: z.number().int().nonnegative(),
    updated_at: z.string().min(1),
})

const securityPageSchema = z.object({
  items: z.array(securitySchema),
  pagination: paginationSchema,
  allowed_actions: z.array(z.literal("REFRESH")).default([]),
})

const dailyBarSchema = z.object({
  trade_date: z.string().min(1),
  open: z.string().min(1),
  high: z.string().min(1),
  low: z.string().min(1),
  close: z.string().min(1),
  volume: z.number().int().nonnegative(),
  amount: z.string().min(1),
})

const dailyBarPageSchema = z.object({
  items: z.array(dailyBarSchema),
  pagination: paginationSchema,
})

const quoteCyclePageSchema = z.object({
  items: z.array(z.object({
    id: z.string().min(1),
    status: z.string().min(1),
    expected_count: z.number().int().nonnegative(),
    valid_count: z.number().int().nonnegative(),
    missing_count: z.number().int().nonnegative(),
    conflict_count: z.number().int().nonnegative(),
    failed_count: z.number().int().nonnegative(),
    scheduled_at: z.string().min(1),
    finalized_at: z.string().nullable(),
  })),
  total: z.number().int().nonnegative(),
  page: z.number().int().positive(),
  page_size: z.number().int().positive(),
  allowed_actions: z.array(
    z.enum(["MANUAL_COLLECT", "DIAGNOSE"]),
  ).default([]),
})

const quoteItemsSchema = z.object({
  items: z.array(z.object({
    id: z.string().min(1),
    symbol: z.string().min(1),
    status: z.string().min(1),
    price: z.string().nullable(),
    provider: z.string().nullable(),
    quote_time: z.string().nullable(),
    error_code: z.string().nullable(),
    eligible_for_evaluation: z.boolean(),
  })),
})

const quoteCheckSchema = z.object({
  status: z.string().min(1),
  mode: z.string().min(1),
  expected_count: z.number().int().nonnegative(),
  valid_count: z.number().int().nonnegative(),
  failed_count: z.number().int().nonnegative(),
  signal_succeeded: z.number().int().nonnegative(),
  signal_failed: z.number().int().nonnegative(),
  completed_at: z.string().min(1),
  failures: z.array(z.object({
    symbol: z.string().min(1),
    code: z.string().min(1),
  })),
})

const dailyBatchPageSchema = z.object({
  items: z.array(z.object({
    id: z.string().min(1),
    trading_date: z.string().min(1),
    status: z.string().min(1),
    expected_count: z.number().int().nonnegative(),
    fetched_count: z.number().int().nonnegative(),
    committed_count: z.number().int().nonnegative(),
    missing_count: z.number().int().nonnegative(),
    failed_count: z.number().int().nonnegative(),
    created_at: z.string().min(1),
    completed_at: z.string().nullable(),
    allowed_actions: z.array(z.literal("RETRY_MISSING")).default([]),
  })),
  pagination: paginationSchema,
})

const qfqSchema = z.object({
  dataset: z.object({
    id: z.string().min(1),
    symbol: z.string().min(1),
    version: z.number().int().positive(),
    actual_start: z.string().min(1),
    actual_end: z.string().min(1),
    as_of_date: z.string().min(1),
    provider: z.string().min(1),
    row_count: z.number().int().nonnegative(),
    lifecycle: z.string().min(1),
    freshness: z.string().min(1),
    stale_reason: z.string().nullable(),
    activated_at: z.string().nullable(),
    allowed_actions: z.array(z.literal("REFRESH")).default([]),
  }),
  items: z.array(dailyBarSchema).default([]),
  pagination: paginationSchema.optional(),
})

const qualityPageSchema = z.object({
  items: z.array(z.object({
    id: z.string().min(1),
    issue_type: z.string().min(1),
    subject_type: z.string().min(1),
    symbol: z.string().nullable(),
    status: z.string().min(1),
    severity: z.string().min(1),
    occurrence_count: z.number().int().nonnegative(),
    last_seen_at: z.string().min(1),
    selected_source: z.string().nullable(),
    source_candidates: z.array(z.string().min(1)),
    allowed_actions: z.array(z.enum(["SELECT_SOURCE", "INVALIDATE", "REFETCH"])),
  })),
  pagination: paginationSchema,
})

const backfillPageSchema = z.object({
  items: z.array(z.object({
    job_id: z.string().min(1),
    status: z.string().min(1),
    progress: z.object({
      completed: z.number().int().nonnegative(),
      total: z.number().int().nonnegative(),
    }).nullable(),
    result_summary: z.object({
      data: z.object({
        succeeded: z.number().int().nonnegative(),
        failed: z.number().int().nonnegative(),
      }).nullable(),
    }).nullable(),
    item_counts: z.object({
      pending: z.number().int().nonnegative(),
      fetching: z.number().int().nonnegative(),
      validating: z.number().int().nonnegative(),
      saving: z.number().int().nonnegative(),
      succeeded: z.number().int().nonnegative(),
      failed: z.number().int().nonnegative(),
      canceled: z.number().int().nonnegative(),
      anomalous: z.number().int().nonnegative().default(0),
    }),
    version: z.number().int().positive(),
    updated_at: z.string().min(1),
    terminal_at: z.string().nullable(),
    allowed_actions: z.array(
      z.enum(["PAUSE", "RESUME", "CANCEL", "RETRY_FAILED"]),
    ).default([]),
  })),
  pagination: paginationSchema,
  allowed_actions: z.array(
    z.enum(["CREATE", "PAUSE", "RESUME", "CANCEL", "RETRY_FAILED"]),
  ).default([]),
})

const backfillItemsSchema = z.object({
  items: z.array(z.object({
    security_id: z.string().min(1),
    symbol: z.string().min(1),
    status: z.enum([
      "PENDING", "RUNNING", "SUCCEEDED", "ANOMALY", "FAILED", "CANCELED",
    ]),
    error_code: z.string().nullable(),
    retryable: z.boolean(),
    anomaly_rows: z.array(z.object({
      trade_date: z.string().min(1),
      error_code: z.string().min(1),
      price_mode: z.string().min(1),
    })),
  })),
  pagination: paginationSchema,
})

function parse<T>(schema: z.ZodType<T>, value: unknown, code: string): T {
  const result = schema.safeParse(value)
  if (!result.success) {
    throw new ApiError("行情接口返回的数据无法识别。", {
      code,
      cause: result.error,
    })
  }
  return result.data
}

function pageInfo(value: z.infer<typeof paginationSchema>) {
  return {
    page: value.page,
    pageSize: value.page_size,
    total: value.total,
  }
}

function securitySummary(
  item: z.infer<typeof securitySchema>,
): SecuritySummary {
  return {
    id: item.symbol,
    symbol: item.symbol,
    name: item.name,
    market: item.market,
    listingStatus: item.listing_status,
    isSt: item.is_st,
    isSuspended: item.is_suspended,
    masterVersion: item.master_version,
    updatedAt: item.updated_at,
  }
}

function priceBar(item: z.infer<typeof dailyBarSchema>): DailyPriceBar {
  return {
    tradeDate: item.trade_date,
    open: item.open,
    high: item.high,
    low: item.low,
    close: item.close,
    volume: item.volume,
    amount: item.amount,
  }
}

function qfqDataset(
  dataset: z.infer<typeof qfqSchema>["dataset"],
): QfqDatasetSummary {
  return {
    id: dataset.id,
    symbol: dataset.symbol,
    version: dataset.version,
    actualStart: dataset.actual_start,
    actualEnd: dataset.actual_end,
    asOfDate: dataset.as_of_date,
    provider: dataset.provider,
    rowCount: dataset.row_count,
    lifecycle: dataset.lifecycle,
    freshness: dataset.freshness,
    staleReason: dataset.stale_reason,
    activatedAt: dataset.activated_at,
    allowedActions: dataset.allowed_actions,
  }
}

function ensureCompleteSeries(
  symbol: string,
  mode: DailyPriceMode,
  pages: Array<{ items: DailyPriceBar[]; total: number }>,
): DailyPriceSeries {
  const total = pages[0]?.total ?? 0
  const items = pages.flatMap((page) => page.items)
  const dates = new Set(items.map((item) => item.tradeDate))
  if (
    pages.some((page) => page.total !== total)
    || items.length !== total
    || dates.size !== items.length
  ) {
    throw new ApiError("日线数据没有完整返回，请重试。", {
      code: "DAILY_SERIES_INCOMPLETE",
    })
  }
  items.sort((left, right) => left.tradeDate.localeCompare(right.tradeDate))
  return { symbol, mode, items, total }
}

export function createMarketDataGateway(baseUrl = ""): MarketDataGateway {
  const api = createApiClient<paths>({ baseUrl })

  return {
    async loadSecurities() {
      const value = await api.request<unknown>(api.client.GET("/api/v1/securities", {
        params: { query: { page: 1, page_size: 50 } },
      }))
      const page = parse(securityPageSchema, value, "SECURITY_LIST_INVALID")
      return {
        items: page.items.map(securitySummary),
        pagination: pageInfo(page.pagination),
        allowedActions: page.allowed_actions,
      }
    },

    async loadSecurityList(filters) {
      const query = filters.query?.trim()
      const operation = query
        ? api.client.GET("/api/v1/securities/search", {
            params: {
              query: {
                q: query,
                page: filters.page,
                page_size: filters.pageSize,
              },
            },
          })
        : api.client.GET("/api/v1/securities", {
            params: {
              query: {
                page: filters.page,
                page_size: filters.pageSize,
              },
            },
          })
      const value = await api.request<unknown>(operation)
      const page = parse(securityPageSchema, value, "SECURITY_LIST_INVALID")
      return {
        items: page.items.map(securitySummary),
        pagination: pageInfo(page.pagination),
      }
    },

    async loadSecurity(symbol) {
      const value = await api.request<unknown>(
        api.client.GET("/api/v1/securities/{symbol}", {
          params: { path: { symbol: symbol.trim().toUpperCase() } },
        }),
      )
      const item = parse(securitySchema, value, "SECURITY_DETAIL_INVALID")
      return {
        ...securitySummary(item),
        exchangeCode: item.exchange_code,
        securityType: item.security_type,
        listedOn: item.listed_on,
        delistedOn: item.delisted_on,
      }
    },

    async loadDailyPrices(command) {
      const symbol = command.symbol.trim().toUpperCase()
      const pageSize = 500
      const loadPage = async (page: number) => {
        if (command.mode === "UNADJUSTED") {
          const value = await api.request<unknown>(
            api.client.GET("/api/v1/daily-bars/{symbol}", {
              params: {
                path: { symbol },
                query: {
                  start: command.startDate,
                  end: command.endDate,
                  page,
                  page_size: pageSize,
                },
              },
            }),
          )
          const parsed = parse(
            dailyBarPageSchema,
            value,
            "DAILY_SERIES_INVALID",
          )
          return {
            items: parsed.items.map(priceBar),
            total: parsed.pagination.total,
          }
        }

        const value = await api.request<unknown>(
          api.client.GET("/api/v1/qfq-data/{symbol}", {
            params: {
              path: { symbol },
              query: {
                start: command.startDate,
                end: command.endDate,
                page,
                page_size: pageSize,
              },
            },
          }),
        )
        const parsed = parse(qfqSchema, value, "QFQ_SERIES_INVALID")
        return {
          items: parsed.items.map(priceBar),
          total: parsed.pagination?.total ?? parsed.items.length,
          dataset: qfqDataset(parsed.dataset),
        }
      }

      const firstPage = await loadPage(1)
      const pageCount = Math.ceil(firstPage.total / pageSize)
      const remainingPages = pageCount > 1
        ? await Promise.all(
            Array.from(
              { length: pageCount - 1 },
              (_, index) => loadPage(index + 2),
            ),
          )
        : []
      const pages = [firstPage, ...remainingPages]
      const result = ensureCompleteSeries(symbol, command.mode, pages)
      if (command.mode === "QFQ") {
        result.dataset = firstPage.dataset
      }
      return result
    },

    async refreshSecurities(reason) {
      await api.request(api.client.POST("/api/v1/securities/refresh", {
        params: {
          header: { "Idempotency-Key": createClientIdempotencyKey() },
        },
        body: { confirm: true, reason },
      }))
    },

    async loadQuoteCycles() {
      const value = await api.request<unknown>(api.client.GET("/api/v1/quote-cycles", {
        params: { query: { page: 1, page_size: 50 } },
      }))
      const page = parse(quoteCyclePageSchema, value, "QUOTE_CYCLE_LIST_INVALID")
      return {
        items: page.items.map((item): QuoteCycleSummary => ({
          id: item.id,
          status: item.status,
          expectedCount: item.expected_count,
          validCount: item.valid_count,
          missingCount: item.missing_count,
          conflictCount: item.conflict_count,
          failedCount: item.failed_count,
          scheduledAt: item.scheduled_at,
          finalizedAt: item.finalized_at,
        })),
        pagination: {
          page: page.page,
          pageSize: page.page_size,
          total: page.total,
        },
        allowedActions: page.allowed_actions,
      }
    },

    async loadQuoteItems(cycleId) {
      const value = await api.request<unknown>(
        api.client.GET("/api/v1/quote-cycles/{cycle_id}/items", {
          params: {
            path: { cycle_id: cycleId },
            query: { page: 1, page_size: 200 },
          },
        }),
      )
      return parse(quoteItemsSchema, value, "QUOTE_ITEM_LIST_INVALID").items
        .map((item): QuoteItemSummary => ({
          id: item.id,
          symbol: item.symbol,
          status: item.status,
          price: item.price,
          provider: item.provider,
          quoteTime: item.quote_time,
          errorCode: item.error_code,
          eligibleForEvaluation: item.eligible_for_evaluation,
        }))
    },

    async runQuoteOperation(command) {
      const request = {
        params: {
          header: { "Idempotency-Key": createClientIdempotencyKey() },
        },
        body: {
          symbols: command.symbols,
          confirm: true as const,
          reason: command.reason,
        },
      }
      let value: unknown
      if (command.action === "MANUAL_COLLECT") {
        value = await api.request(api.client.POST("/api/v1/quotes/check-now", {
          ...request,
          body: {
            ...request.body,
            timeout_seconds: command.timeoutSeconds ?? 30,
          },
        }))
      } else {
        value = await api.request(api.client.POST("/api/v1/quotes/diagnose", request))
      }
      const result = parse(quoteCheckSchema, value, "QUOTE_CHECK_RESULT_INVALID")
      return {
        status: result.status,
        mode: result.mode,
        expectedCount: result.expected_count,
        validCount: result.valid_count,
        failedCount: result.failed_count,
        signalSucceeded: result.signal_succeeded,
        signalFailed: result.signal_failed,
        failures: result.failures,
        completedAt: result.completed_at,
      } satisfies QuoteCheckResult
    },

    async loadDailyBatches() {
      const value = await api.request<unknown>(
        api.client.GET("/api/v1/daily-data/batches", {
          params: { query: { page: 1, page_size: 50 } },
        }),
      )
      const page = parse(dailyBatchPageSchema, value, "DAILY_BATCH_LIST_INVALID")
      return {
        items: page.items.map((item): DailyBatchSummary => ({
          id: item.id,
          tradingDate: item.trading_date,
          status: item.status,
          expectedCount: item.expected_count,
          fetchedCount: item.fetched_count,
          committedCount: item.committed_count,
          missingCount: item.missing_count,
          failedCount: item.failed_count,
          createdAt: item.created_at,
          completedAt: item.completed_at,
          allowedActions: item.allowed_actions,
        })),
        pagination: pageInfo(page.pagination),
      }
    },

    async retryDailyBatch(command) {
      await api.request(api.client.POST(
        "/api/v1/daily-data/batches/{batch_id}/retry",
        {
          params: {
            path: { batch_id: command.batchId },
            header: { "Idempotency-Key": createClientIdempotencyKey() },
          },
          body: {
            confirm: true,
            reason: command.reason,
          },
        },
      ))
    },

    async loadQfq(symbol) {
      const value = await api.request<unknown>(
        api.client.GET("/api/v1/qfq-data/{symbol}", {
          params: {
            path: { symbol },
            query: { page: 1, page_size: 1 },
          },
        }),
      )
      const dataset = parse(qfqSchema, value, "QFQ_DATASET_INVALID").dataset
      return qfqDataset(dataset)
    },

    async refreshQfq(command) {
      await api.request(api.client.POST("/api/v1/qfq-data/{symbol}/refresh", {
        params: {
          path: { symbol: command.dataset.symbol },
          header: { "Idempotency-Key": createClientIdempotencyKey() },
        },
        body: {
          start: command.dataset.actualStart,
          end: command.dataset.actualEnd,
          as_of_date: command.dataset.asOfDate,
          confirm: true,
          reason: command.reason,
          expected_version: command.dataset.version,
        },
      }))
    },

    async loadQualityIssues() {
      const value = await api.request<unknown>(
        api.client.GET("/api/v1/data-quality/issues", {
          params: { query: { page: 1, page_size: 50 } },
        }),
      )
      const page = parse(qualityPageSchema, value, "QUALITY_ISSUE_LIST_INVALID")
      return {
        items: page.items.map((item): QualityIssueSummary => ({
          id: item.id,
          issueType: item.issue_type,
          subjectType: item.subject_type,
          symbol: item.symbol,
          status: item.status,
          severity: item.severity,
          occurrenceCount: item.occurrence_count,
          lastSeenAt: item.last_seen_at,
          selectedSource: item.selected_source,
          sourceCandidates: item.source_candidates,
          allowedActions: item.allowed_actions,
        })),
        pagination: pageInfo(page.pagination),
      }
    },

    async runQualityAction(command) {
      const common = {
        params: {
          path: { issue_id: command.issueId },
          header: { "Idempotency-Key": createClientIdempotencyKey() },
        },
        body: {
          confirm: true as const,
          reason: command.reason,
        },
      }
      if (command.action === "SELECT_SOURCE") {
        if (!command.selectedSource) {
          throw new ApiError("请选择服务端提供的数据来源。", {
            code: "QUALITY_SOURCE_REQUIRED",
          })
        }
        await api.request(api.client.POST(
          "/api/v1/data-quality/issues/{issue_id}/select-source",
          {
            ...common,
            body: {
              ...common.body,
              selected_source: command.selectedSource,
            },
          },
        ))
        return
      }
      if (command.action === "INVALIDATE") {
        await api.request(api.client.POST(
          "/api/v1/data-quality/issues/{issue_id}/resolve",
          common,
        ))
        return
      }
      await api.request(api.client.POST(
        "/api/v1/data-quality/issues/{issue_id}/refetch",
        common,
      ))
    },

    async loadBackfills() {
      const value = await api.request<unknown>(
        api.client.GET("/api/v1/market-history/backfills", {
          params: { query: { page: 1, page_size: 50 } },
        }),
      )
      const page = parse(backfillPageSchema, value, "BACKFILL_LIST_INVALID")
      return {
        items: page.items.map((item): BackfillSummary => ({
          id: item.job_id,
          status: item.status,
          version: item.version,
          completed: item.progress?.completed ?? 0,
          total: item.progress?.total ?? 0,
          succeeded: item.result_summary?.data?.succeeded ?? null,
          failed: item.result_summary?.data?.failed ?? null,
          itemCounts: item.item_counts,
          updatedAt: item.updated_at,
          terminalAt: item.terminal_at,
          allowedActions: item.allowed_actions,
        })),
        pagination: pageInfo(page.pagination),
        allowedActions: page.allowed_actions,
      }
    },

    async createBackfill(command) {
      await api.request(api.client.POST("/api/v1/market-history/backfills", {
        params: {
          header: { "Idempotency-Key": createClientIdempotencyKey() },
        },
        body: {
          scope: command.scope,
          symbols: command.symbols,
          start_date: command.startDate ?? null,
          end_date: command.endDate ?? null,
          concurrency: command.concurrency,
          watchlist_id: null,
          confirm: true,
          reason: command.reason,
        },
      }))
    },

    async runBackfillAction(command) {
      const path = {
        PAUSE: "/api/v1/market-history/backfills/{job_id}/pause",
        RESUME: "/api/v1/market-history/backfills/{job_id}/resume",
        CANCEL: "/api/v1/market-history/backfills/{job_id}/cancel",
        RETRY_FAILED:
          "/api/v1/market-history/backfills/{job_id}/retry-failed",
      } as const
      await api.request(api.client.POST(path[command.action], {
        params: {
          path: { job_id: command.job.id },
          header: { "Idempotency-Key": createClientIdempotencyKey() },
        },
        body: {
          confirm: true,
          reason: command.reason,
          expected_version: command.job.version,
        },
      }))
    },

    async loadBackfillItems(command) {
      const value = await api.request<unknown>(api.client.GET(
        "/api/v1/market-history/backfills/{job_id}/items",
        {
          params: {
            path: { job_id: command.jobId },
            query: {
              status: command.status ?? null,
              page: command.page,
              page_size: command.pageSize,
            },
          },
        },
      ))
      const page = parse(backfillItemsSchema, value, "BACKFILL_ITEMS_INVALID")
      return {
        items: page.items.map((item): BackfillItemSummary => ({
          securityId: item.security_id,
          symbol: item.symbol,
          status: item.status,
          errorCode: item.error_code,
          retryable: item.retryable,
          anomalyRows: item.anomaly_rows,
        })),
        pagination: pageInfo(page.pagination),
      }
    },

    async retryBackfillItems(command) {
      await api.request(api.client.POST(
        "/api/v1/market-history/backfills/{job_id}/items/retry",
        {
          params: {
            path: { job_id: command.jobId },
            header: { "Idempotency-Key": createClientIdempotencyKey() },
          },
          body: {
            symbols: command.symbols,
            provider_code: command.providerCode ?? null,
            concurrency: command.concurrency,
            confirm: true,
            reason: command.reason,
          },
        },
      ))
    },
  }
}

export const marketDataGateway = createMarketDataGateway()
