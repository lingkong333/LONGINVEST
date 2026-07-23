import type { paths } from "@/shared/api/generated/schema"
import { ApiError, createApiClient, createClientIdempotencyKey } from "@/shared/api/client"

import type {
  BacktestAction,
  BacktestControlResultDto,
  BacktestMetricsDto,
  BacktestSummaryDto,
  BacktestTaskListItemDto,
  DraftRevision,
  HoldoutBacktestInput,
  HoldoutBacktestResult,
  StrategyAction,
  StrategyApi,
  StrategyDraft,
  StrategyPublishInput,
  StrategyListItem,
  StrategyRunResult,
  StrategyTestInput,
  StrategyValidationInput,
  StrategyVersion,
} from "./types"

type ApiClient = ReturnType<typeof createApiClient<paths>>
type JsonRecord = Record<string, unknown>

const strategyActions = new Set<StrategyAction>(["validate", "test", "publish", "archive"])
const backtestActions = new Set<BacktestAction>(["PAUSE", "RESUME", "CANCEL", "RETRY_FAILED", "RERUN"])

function record(value: unknown): JsonRecord {
  return typeof value === "object" && value !== null ? value as JsonRecord : {}
}

function array(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" || typeof value === "number" ? String(value) : fallback
}

function integer(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isInteger(value) ? value : fallback
}

function nullableText(value: unknown): string | null {
  return typeof value === "string" ? value : null
}

function strategyAllowedActions(value: unknown): StrategyAction[] {
  return array(record(value).allowed_actions)
    .map((action) => text(action).toLowerCase())
    .filter((action): action is StrategyAction => strategyActions.has(action as StrategyAction))
}

function hasStrategyAction(value: unknown, action: string): boolean {
  return array(record(value).allowed_actions).some((item) => text(item).toUpperCase() === action)
}

function backtestAllowedActions(value: unknown): BacktestAction[] {
  return array(record(value).allowed_actions)
    .filter((action): action is BacktestAction =>
      typeof action === "string" && backtestActions.has(action as BacktestAction),
    )
}

function parseJsonObject(value: string): JsonRecord {
  try {
    return record(JSON.parse(value))
  } catch {
    return {}
  }
}

function jsonText(value: unknown, fallback = "{}"): string {
  if (typeof value === "string") return value
  if (typeof value !== "object" || value === null || Array.isArray(value)) return fallback
  return JSON.stringify(value, null, 2)
}

function runResult(value: unknown, sourceVersion: number): StrategyRunResult {
  const source = record(value)
  const rawStatus = text(source.status, "PENDING")
  const status = ["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELED"].includes(rawStatus)
    ? rawStatus as StrategyRunResult["status"]
    : "PENDING"
  return {
    id: text(source.id) || text(source.run_id) || undefined,
    status,
    sourceVersion: integer(source.draft_version, sourceVersion),
    summary: text(source.summary) || text(source.error_code) || undefined,
    details: array(source.details).filter((item): item is string => typeof item === "string"),
  }
}

function draft(value: unknown, strategyValue: unknown): StrategyDraft {
  const source = record(value)
  const strategy = record(strategyValue)
  const metadata = record(source.metadata)
  const actionSource = array(source.allowed_actions).length ? source : strategy
  return {
    id: text(source.id),
    strategyId: text(source.strategy_id, text(strategy.id)),
    name: text(source.name, text(strategy.name, "未命名策略")),
    description: text(metadata.description, text(source.description)),
    metadata,
    sourceCode: text(source.source_code),
    parameterSchema: jsonText(source.parameter_schema),
    version: integer(source.draft_version, integer(source.version, 1)),
    strategyVersion: integer(strategy.version, integer(source.strategy_version, 1)),
    updatedAt: text(source.updated_at),
    allowedActions: strategyAllowedActions(actionSource),
    canSave: hasStrategyAction(actionSource, "SAVE_DRAFT"),
    canRestoreRevision: hasStrategyAction(actionSource, "RESTORE_REVISION"),
    validationResult: source.validation_result ? runResult(source.validation_result, integer(source.draft_version, 1)) : undefined,
    testResult: source.test_result ? runResult(source.test_result, integer(source.draft_version, 1)) : undefined,
  }
}

function revision(value: unknown): DraftRevision {
  const source = record(value)
  const metadata = record(source.metadata)
  return {
    id: text(source.id),
    revisionNo: integer(source.revision_no),
    description: text(metadata.description),
    metadata,
    sourceCode: text(source.source_code),
    parameterSchema: jsonText(source.parameter_schema),
    createdAt: text(source.created_at),
  }
}

function strategyItem(value: unknown): StrategyListItem {
  const source = record(value)
  return { id: text(source.id), name: text(source.name, "未命名策略"), status: text(source.status) }
}

function version(value: unknown): StrategyVersion {
  const source = record(value)
  const rawStatus = text(source.status, "PUBLISHING")
  return {
    id: text(source.id),
    versionNo: integer(source.version_no),
    status: ["PUBLISHING", "PUBLISHED", "PUBLISH_FAILED", "ARCHIVED"].includes(rawStatus)
      ? rawStatus as StrategyVersion["status"]
      : "PUBLISHING",
    sourceCodeHash: text(source.source_code_hash),
    sourceCode: text(source.source_code) || undefined,
    publishedAt: nullableText(source.published_at),
  }
}

function dateRange(value: unknown) {
  const source = record(value)
  return {
    trainingStartDate: text(source.training_start_date),
    trainingEndDate: text(source.training_end_date),
    testStartDate: text(source.test_start_date),
    testEndDate: text(source.test_end_date),
  }
}

function taskItem(value: unknown): BacktestTaskListItemDto {
  const source = record(value)
  const item = record(source.item)
  return {
    taskId: text(source.task_id),
    rerunFromTaskId: nullableText(source.rerun_from_task_id),
    mode: text(source.mode, "SINGLE") as BacktestTaskListItemDto["mode"],
    status: text(source.status),
    dateRange: dateRange(source.date_range),
    item: {
      itemId: text(item.item_id),
      securityId: text(item.security_id),
      symbol: text(item.symbol),
      name: text(item.name),
      status: text(item.status, text(item.item_status)),
      failureCode: nullableText(item.failure_code),
      attemptCount: integer(item.attempt_count),
      startedAt: nullableText(item.started_at),
      endedAt: nullableText(item.ended_at),
    },
    allowedActions: backtestAllowedActions(source),
    createdAt: text(source.created_at),
    updatedAt: text(source.updated_at),
    terminalAt: nullableText(source.terminal_at),
  }
}

function metric(value: unknown): BacktestMetricsDto | null {
  if (!value) return null
  const source = record(value)
  return {
    itemId: text(source.item_id),
    endingEquity: text(source.ending_equity),
    totalReturn: text(source.total_return),
    realizedReturn: text(source.realized_return),
    annualizedReturn: text(source.annualized_return),
    maxDrawdown: text(source.max_drawdown),
    volatility: text(source.volatility),
    sharpeRatio: nullableText(source.sharpe_ratio),
    completedRoundTrips: integer(source.completed_round_trips),
    winningTrades: integer(source.winning_trades),
    losingTrades: integer(source.losing_trades),
    breakevenTrades: integer(source.breakeven_trades),
    winRate: nullableText(source.win_rate),
    averageTradeReturn: nullableText(source.average_trade_return),
    maximumTradeGain: nullableText(source.maximum_trade_gain),
    maximumTradeLoss: nullableText(source.maximum_trade_loss),
    averageHoldingTradeDays: nullableText(source.average_holding_trade_days),
    longestHoldingTradeDays: integer(source.longest_holding_trade_days),
    capitalExposureRatio: text(source.capital_exposure_ratio),
    openPositionAtEnd: source.open_position_at_end === true,
    unfilledOrderCount: integer(source.unfilled_order_count),
  }
}

function targetValues(value: unknown) {
  const source = record(value)
  return {
    lowStrong: text(source.low_strong),
    lowWatch: text(source.low_watch),
    highWatch: text(source.high_watch),
    highStrong: text(source.high_strong),
  }
}

function result(value: unknown, taskId: string, fallbackStatus = "PENDING"): HoldoutBacktestResult {
  const source = record(value)
  const item = source.item ? record(source.item) : source
  const hasItem = Boolean(text(item.id) || text(item.item_id))
  const forecast = source.forecast ? record(source.forecast) : null
  return {
    id: text(source.task_id, taskId),
    status: fallbackStatus,
    item: hasItem ? {
      id: text(item.id, text(item.item_id)),
      taskId: text(item.task_id, taskId),
      securityId: text(item.security_id),
      status: text(item.status, text(item.item_status)),
      failureCode: text(item.failure_code) || undefined,
      failureMessage: text(item.failure_message) || undefined,
    } : undefined,
    forecast: forecast ? {
      itemId: text(forecast.item_id),
      trainingStartDate: text(forecast.training_start_date),
      trainingEndDate: text(forecast.training_end_date),
      trainingRowCount: integer(forecast.training_row_count),
      trainingFetchedAt: text(forecast.training_fetched_at),
      trainingDataHash: text(forecast.training_data_hash),
      sourceCodeHash: text(forecast.source_code_hash),
      parameterHash: text(forecast.parameter_hash),
      values: targetValues(forecast.values),
      diagnostics: record(forecast.diagnostics),
      environmentVersion: text(forecast.environment_version),
      runnerImageDigest: text(forecast.runner_image_digest),
      priceBasis: text(forecast.price_basis),
      frozenAt: text(forecast.frozen_at),
    } : null,
    adjustments: array(source.adjustments).map((itemValue) => {
      const adjustment = record(itemValue)
      return {
        itemId: text(adjustment.item_id),
        eventDate: text(adjustment.event_date),
        beforeValues: targetValues(adjustment.before_values),
        afterValues: targetValues(adjustment.after_values),
        adjustmentFactor: text(adjustment.adjustment_factor),
        source: text(adjustment.source),
        dataHash: text(adjustment.data_hash),
        publishedAt: text(adjustment.published_at),
        effectiveAt: text(adjustment.effective_at),
      }
    }),
    orders: array(source.orders).map((itemValue) => {
      const order = record(itemValue)
      return {
        id: text(order.id), itemId: text(order.item_id), signalDate: text(order.signal_date),
        executeDate: nullableText(order.execute_date), status: text(order.status) as "PENDING" | "FILLED" | "UNFILLED_AT_END",
        direction: text(order.direction) as "BUY" | "SELL", executionPrice: nullableText(order.execution_price),
        quantity: text(order.quantity), cashBefore: text(order.cash_before), positionBefore: text(order.position_before),
        targetValues: targetValues(order.target_values), targetZone: text(order.target_zone),
      }
    }),
    trades: array(source.trades).map((itemValue) => {
      const trade = record(itemValue)
      return {
        id: text(trade.id), itemId: text(trade.item_id), orderId: text(trade.order_id),
        executeDate: text(trade.execute_date), direction: text(trade.direction) as "BUY" | "SELL",
        price: text(trade.price), quantity: text(trade.quantity), cashAfter: text(trade.cash_after),
        positionAfter: text(trade.position_after), targetValues: targetValues(trade.target_values),
        targetZone: text(trade.target_zone), roundTripNo: integer(trade.round_trip_no),
        holdingTradeDays: typeof trade.holding_trade_days === "number" ? trade.holding_trade_days : null,
        realizedReturnAmount: nullableText(trade.realized_return_amount),
        realizedReturnRate: nullableText(trade.realized_return_rate),
      }
    }),
    metrics: metric(source.metric),
    dailyResults: array(source.daily_results).map((itemValue) => {
      const daily = record(itemValue)
      return {
        itemId: text(daily.item_id), tradeDate: text(daily.trade_date), cash: text(daily.cash),
        positionQuantity: text(daily.position_quantity), closePrice: text(daily.close_price),
        positionMarketValue: text(daily.position_market_value), equity: text(daily.equity),
        drawdown: text(daily.drawdown), targetValues: targetValues(daily.target_values),
        zone: text(daily.zone), positionStatus: text(daily.position_status) as "FLAT" | "HOLDING",
      }
    }),
    failureMessage: text(source.failure_message) || undefined,
  }
}

async function loadDraft(api: ApiClient, strategyId: string): Promise<StrategyDraft> {
  const [strategyValue, draftValue] = await Promise.all([
    api.request(api.client.GET("/api/v1/strategies/{strategy_id}", {
      params: { path: { strategy_id: strategyId } },
    })),
    api.request(api.client.GET("/api/v1/strategies/{strategy_id}/draft", {
      params: { path: { strategy_id: strategyId } },
    })),
  ])
  return draft(draftValue, strategyValue)
}

export function createStrategyApi(api = createApiClient<paths>()): StrategyApi {
  return {
    async listStrategies() {
      const value = record(await api.request(api.client.GET("/api/v1/strategies", {
        params: { query: { page: 1, page_size: 100, include_archived: true } },
      })))
      return {
        items: array(value.items).map(strategyItem),
        canCreate: array(value.allowed_actions).some((action) => text(action).toUpperCase() === "CREATE"),
      }
    },
    async createStrategy(name, reason) {
      const value = record(await api.request(api.client.POST("/api/v1/strategies", {
        body: { confirm: true, name, reason },
      })))
      return strategyItem(value.strategy)
    },
    getDraft: (strategyId) => loadDraft(api, strategyId),
    async saveDraft(strategyId, input) {
      try {
        const current = await loadDraft(api, strategyId)
        if (input.name !== current.name) {
          throw new ApiError("策略名称需要通过重命名操作修改。", {
            code: "STRATEGY_DRAFT_FIELDS_UNSUPPORTED",
          })
        }
        const value = await api.request(api.client.PUT("/api/v1/strategies/{strategy_id}/draft", {
          params: {
            path: { strategy_id: strategyId },
          },
          body: {
            confirm: true,
            reason: "保存策略草稿",
            source_code: input.sourceCode,
            metadata: { ...current.metadata, description: input.description },
            parameter_schema: parseJsonObject(input.parameterSchema),
            expected_version: input.expectedVersion,
          },
        }))
        return draft(value, current)
      } catch (error) {
        if (!(error instanceof ApiError) || error.status !== 409) throw error
        throw { status: 409, current: await loadDraft(api, strategyId) }
      }
    },
    async listRevisions(strategyId) {
      const value = record(await api.request(api.client.GET("/api/v1/strategies/{strategy_id}/draft/revisions", {
        params: { path: { strategy_id: strategyId }, query: { page: 1, page_size: 100 } },
      })))
      return array(value.items).map(revision)
    },
    async restoreRevision(strategyId, revisionId, reason, idempotencyKey) {
      const current = await loadDraft(api, strategyId)
      const value = await api.request(api.client.POST("/api/v1/strategies/{strategy_id}/draft/revisions/{revision_id}/restore", {
        params: {
          path: { strategy_id: strategyId, revision_id: revisionId },
        },
        headers: { "Idempotency-Key": idempotencyKey },
        body: { confirm: true, reason, expected_version: current.version },
      }))
      return draft(value, current)
    },
    async validateDraft(strategyId, input: StrategyValidationInput) {
      const current = await loadDraft(api, strategyId)
      const value = await api.request(api.client.POST("/api/v1/strategies/{strategy_id}/validate", {
        params: {
          path: { strategy_id: strategyId },
        },
        body: {
          confirm: true, reason: input.reason, backtest_task_id: input.backtestTaskId,
          params: input.params,
        },
      }))
      return runResult(value, current.version)
    },
    async testDraft(strategyId, input: StrategyTestInput) {
      const current = await loadDraft(api, strategyId)
      const value = await api.request(api.client.POST("/api/v1/strategies/{strategy_id}/test", {
        params: {
          path: { strategy_id: strategyId },
        },
        body: {
          confirm: true, reason: input.reason, symbol: input.symbol,
          training_start_date: input.trainingStartDate, training_end_date: input.trainingEndDate,
          test_start_date: input.testStartDate, test_end_date: input.testEndDate,
          parameter_snapshot: input.parameterSnapshot, initial_capital: input.initialCapital,
        },
      }))
      return runResult(value, current.version)
    },
    async publishDraft(strategyId, input: StrategyPublishInput) {
      const value = await api.request(api.client.POST("/api/v1/strategies/{strategy_id}/publish", {
        params: {
          path: { strategy_id: strategyId },
        },
        body: {
          confirm: true, reason: input.reason, validation_run_id: input.validationRunId,
          expected_draft_version: input.expectedDraftVersion,
        },
      }))
      return runResult(value, input.expectedDraftVersion)
    },
    async archiveStrategy(strategyId, reason, expectedVersion) {
      const value = await api.request(api.client.POST("/api/v1/strategies/{strategy_id}/archive", {
        params: {
          path: { strategy_id: strategyId },
        },
        body: { confirm: true, reason, expected_version: expectedVersion },
      }))
      return runResult(value, expectedVersion)
    },
    async listVersions(strategyId) {
      const value = record(await api.request(api.client.GET("/api/v1/strategies/{strategy_id}/versions", {
        params: { path: { strategy_id: strategyId }, query: { page: 1, page_size: 100 } },
      })))
      return array(value.items).map(version)
    },
    async createHoldoutBacktest(input: HoldoutBacktestInput) {
      const current = await loadDraft(api, input.strategyId)
      const value = record(await api.request(api.client.POST("/api/v1/backtests", {
        params: { header: { "Idempotency-Key": createClientIdempotencyKey() } },
        body: {
          mode: "SINGLE", symbol: input.securitySymbol,
          date_range: {
            training_start_date: input.trainingStartDate, training_end_date: input.trainingEndDate,
            test_start_date: input.testStartDate, test_end_date: input.testEndDate,
          },
          draft_id: current.id, draft_version: current.version,
          strategy_metadata: current.metadata, parameter_schema: parseJsonObject(current.parameterSchema),
          parameter_snapshot: input.parameterSnapshot ?? {}, initial_capital: input.initialCapital ?? "100000",
          confirm: true, reason: "创建单股样本外回测",
        },
      })))
      const task = record(value.task)
      return result(value, text(task.id, text(task.task_id)), text(value.item_status, "PENDING"))
    },
    async listHoldoutBacktests(strategyId) {
      const [draftValue, versionsValue, tasksValue] = await Promise.all([
        loadDraft(api, strategyId),
        api.request(api.client.GET("/api/v1/strategies/{strategy_id}/versions", {
          params: { path: { strategy_id: strategyId }, query: { page: 1, page_size: 100 } },
        })),
        api.request(api.client.GET("/api/v1/backtests", {
          params: { query: { page: 1, page_size: 200 } },
        })),
      ])
      const value = record(tasksValue)
      const versionIds = new Set(array(record(versionsValue).items).map((item) => text(record(item).id)))
      const strategyItems = array(value.items).filter((item) => {
        const task = record(item)
        return text(task.draft_id) === draftValue.id || versionIds.has(text(task.strategy_version_id))
      })
      const pagination = record(value.pagination)
      return {
        items: strategyItems.map(taskItem),
        page: integer(pagination.page, 1),
        pageSize: integer(pagination.page_size, 200),
        total: strategyItems.length,
      }
    },
    async getHoldoutBacktest(backtestId) {
      const [itemsResponse, summaryResponse] = await Promise.all([
        api.request(api.client.GET("/api/v1/backtests/{task_id}/items", {
          params: { path: { task_id: backtestId }, query: { page: 1, page_size: 1 } },
        })),
        api.request(api.client.GET("/api/v1/backtests/{task_id}/summary", {
          params: { path: { task_id: backtestId } },
        })),
      ])
      const itemsValue = record(itemsResponse)
      const summaryValue = record(summaryResponse)
      const firstItem = record(array(itemsValue.items)[0])
      const itemId = text(firstItem.item_id)
      const taskStatus = text(summaryValue.status, "PENDING")
      if (!itemId) return result({}, backtestId, taskStatus)
      const value = await api.request(api.client.GET("/api/v1/backtests/{task_id}/items/{item_id}", {
        params: { path: { task_id: backtestId, item_id: itemId } },
      }))
      return result(value, backtestId, taskStatus)
    },
    async getHoldoutBacktestSummary(backtestId) {
      const source = record(await api.request(api.client.GET("/api/v1/backtests/{task_id}/summary", {
        params: { path: { task_id: backtestId } },
      })))
      return {
        taskId: text(source.task_id), status: text(source.status),
        totalItems: integer(source.total_items), completedItems: integer(source.completed_items),
        succeededItems: integer(source.succeeded_items), failedItems: integer(source.failed_items),
        canceledItems: integer(source.canceled_items), pendingItems: integer(source.pending_items),
        failureCodes: Object.fromEntries(Object.entries(record(source.failure_codes))
          .filter((entry): entry is [string, number] => typeof entry[1] === "number")),
        allowedActions: backtestAllowedActions(source), metric: metric(source.metric),
      } satisfies BacktestSummaryDto
    },
    async controlHoldoutBacktest(backtestId, action, reason) {
      const path = {
        PAUSE: "/api/v1/backtests/{task_id}/pause",
        RESUME: "/api/v1/backtests/{task_id}/resume",
        CANCEL: "/api/v1/backtests/{task_id}/cancel",
        RETRY_FAILED: "/api/v1/backtests/{task_id}/retry-failed",
        RERUN: "/api/v1/backtests/{task_id}/rerun",
      }[action]
      const options = {
        params: {
          path: { task_id: backtestId },
          header: { "Idempotency-Key": createClientIdempotencyKey() },
        },
        body: { confirm: true, reason },
      }
      let value: unknown
      if (path === "/api/v1/backtests/{task_id}/pause") value = await api.request(api.client.POST(path, options))
      else if (path === "/api/v1/backtests/{task_id}/resume") value = await api.request(api.client.POST(path, options))
      else if (path === "/api/v1/backtests/{task_id}/cancel") value = await api.request(api.client.POST(path, options))
      else if (path === "/api/v1/backtests/{task_id}/retry-failed") value = await api.request(api.client.POST(path, options))
      else value = await api.request(api.client.POST("/api/v1/backtests/{task_id}/rerun", options))
      const source = record(value)
      return {
        taskId: text(source.task_id, backtestId),
        status: text(source.status),
        allowedActions: backtestAllowedActions(source),
      } satisfies BacktestControlResultDto
    },
  }
}

export const strategyGatewayInternals = {
  backtestAllowedActions,
  draft,
  result,
  runResult,
  strategyAllowedActions,
  taskItem,
}
