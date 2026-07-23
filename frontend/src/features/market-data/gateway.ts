import { z } from "zod"

import type {
  BackfillSummary,
  DailyBatchSummary,
  MarketDataGateway,
  QfqDatasetSummary,
  QualityIssueSummary,
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

const securityPageSchema = z.object({
  items: z.array(z.object({
    symbol: z.string().min(1),
    name: z.string().min(1),
    market: z.string().min(1),
    listing_status: z.string().min(1),
    is_st: z.boolean(),
    is_suspended: z.boolean(),
    master_version: z.number().int().nonnegative(),
    updated_at: z.string().min(1),
  })),
  pagination: paginationSchema,
  allowed_actions: z.array(z.literal("REFRESH")).default([]),
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

export function createMarketDataGateway(baseUrl = ""): MarketDataGateway {
  const api = createApiClient<paths>({ baseUrl })

  return {
    async loadSecurities() {
      const value = await api.request<unknown>(api.client.GET("/api/v1/securities", {
        params: { query: { page: 1, page_size: 50 } },
      }))
      const page = parse(securityPageSchema, value, "SECURITY_LIST_INVALID")
      return {
        items: page.items.map((item): SecuritySummary => ({
          id: item.symbol,
          symbol: item.symbol,
          name: item.name,
          market: item.market,
          listingStatus: item.listing_status,
          isSt: item.is_st,
          isSuspended: item.is_suspended,
          masterVersion: item.master_version,
          updatedAt: item.updated_at,
        })),
        pagination: pageInfo(page.pagination),
        allowedActions: page.allowed_actions,
      }
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
      if (command.action === "MANUAL_COLLECT") {
        await api.request(api.client.POST("/api/v1/quote-cycles/manual", {
          ...request,
          body: {
            ...request.body,
            timeout_seconds: command.timeoutSeconds ?? 30,
          },
        }))
        return
      }
      await api.request(api.client.POST("/api/v1/quotes/diagnose", request))
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
      } satisfies QfqDatasetSummary
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
          start_date: command.startDate,
          end_date: command.endDate,
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
  }
}

export const marketDataGateway = createMarketDataGateway()
