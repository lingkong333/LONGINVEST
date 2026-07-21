import type { ComponentType } from "react"

export interface CodeEditorProps {
  value: string
  onChange: (value: string) => void
  language: "python"
  ariaLabel: string
  height: string
}

export interface DiffViewerProps {
  original: string
  modified: string
  language: "python"
  originalLabel: string
  modifiedLabel: string
}

export interface StrategyEditorComponents {
  CodeEditor: ComponentType<CodeEditorProps>
  DiffViewer: ComponentType<DiffViewerProps>
}

export type StrategyAction = "validate" | "test" | "publish" | "archive"

export interface StrategyRunResult {
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "CANCELED"
  sourceVersion: number
  summary?: string
  details?: string[]
}

export interface StrategyDraft {
  id: string
  strategyId: string
  name: string
  description: string
  sourceCode: string
  parameterSchema: string
  version: number
  updatedAt: string
  allowedActions: StrategyAction[]
  validationResult?: StrategyRunResult
  testResult?: StrategyRunResult
}

export interface DraftRevision {
  id: string
  revisionNo: number
  sourceCode: string
  createdAt: string
}

export interface StrategyVersion {
  id: string
  versionNo: number
  status: "PUBLISHING" | "PUBLISHED" | "PUBLISH_FAILED" | "ARCHIVED"
  sourceCodeHash: string
  sourceCode?: string
  publishedAt: string | null
}

export interface DraftSaveInput {
  name: string
  description: string
  sourceCode: string
  parameterSchema: string
  expectedVersion: number
}

export interface SaveConflict {
  status: 409
  current: StrategyDraft
}

export interface HoldoutBacktestInput {
  strategyId: string
  securitySymbol: string
  trainingStartDate: string
  trainingEndDate: string
  testStartDate: string
  testEndDate: string
}

export type BacktestTaskStatus =
  | "PENDING"
  | "RUNNING"
  | "PAUSING"
  | "PAUSED"
  | "SUCCEEDED"
  | "PARTIAL"
  | "FAILED"
  | "CANCELING"
  | "CANCELED"

export type BacktestItemStatus =
  | "PENDING"
  | "FETCHING_DATA"
  | "VALIDATING_DATA"
  | "FORECASTING"
  | "FROZEN"
  | "SIMULATING"
  | "SAVING"
  | "SUCCEEDED"
  | "FAILED"
  | "SKIPPED"
  | "CANCELED"

export interface TargetValuesDto {
  lowStrong: string
  lowWatch: string
  highWatch: string
  highStrong: string
}

export interface BacktestForecastDto {
  itemId: string
  trainingStartDate: string
  trainingEndDate: string
  trainingRowCount: number
  trainingFetchedAt: string
  trainingDataHash: string
  sourceCodeHash: string
  parameterHash: string
  values: TargetValuesDto
  diagnostics: Record<string, unknown>
  environmentVersion: string
  runnerImageDigest: string
  priceBasis: string
  frozenAt: string
}

export interface BacktestAdjustmentDto {
  itemId: string
  eventDate: string
  beforeValues: TargetValuesDto
  afterValues: TargetValuesDto
  adjustmentFactor: string
  source: string
  dataHash: string
  publishedAt: string
  effectiveAt: string
}

export interface BacktestOrderDto {
  id: string
  itemId: string
  signalDate: string
  executeDate: string | null
  status: "PENDING" | "FILLED" | "UNFILLED_AT_END"
  direction: "BUY" | "SELL"
  executionPrice: string | null
  quantity: string
  cashBefore: string
  positionBefore: string
  targetValues: TargetValuesDto
  targetZone: string
}

export interface BacktestTradeDto {
  id: string
  itemId: string
  orderId: string
  executeDate: string
  direction: "BUY" | "SELL"
  price: string
  quantity: string
  cashAfter: string
  positionAfter: string
  targetValues: TargetValuesDto
  targetZone: string
  roundTripNo: number
  holdingTradeDays: number | null
  realizedReturnAmount: string | null
  realizedReturnRate: string | null
}

export interface BacktestMetricsDto {
  itemId: string
  endingEquity: string
  totalReturn: string
  realizedReturn: string
  annualizedReturn: string
  maxDrawdown: string
  volatility: string
  sharpeRatio: string | null
  completedRoundTrips: number
  winningTrades: number
  losingTrades: number
  breakevenTrades: number
  winRate: string | null
  averageTradeReturn: string | null
  maximumTradeGain: string | null
  maximumTradeLoss: string | null
  averageHoldingTradeDays: string | null
  longestHoldingTradeDays: number
  capitalExposureRatio: string
  openPositionAtEnd: boolean
  unfilledOrderCount: number
}

export interface BacktestItemDto {
  id?: string
  taskId?: string
  securityId?: string
  status: BacktestItemStatus | string
  failureCode?: string
  failureMessage?: string
}

export interface BacktestDailyResultDto {
  itemId: string
  tradeDate: string
  cash: string
  positionQuantity: string
  closePrice: string
  positionMarketValue: string
  equity: string
  drawdown: string
  targetValues: TargetValuesDto
  zone: string
  positionStatus: "FLAT" | "HOLDING"
}

export interface HoldoutBacktestResult {
  id: string
  status: BacktestTaskStatus | string
  item?: BacktestItemDto
  forecast: BacktestForecastDto | null
  adjustments: BacktestAdjustmentDto[]
  orders: BacktestOrderDto[]
  trades: BacktestTradeDto[]
  metrics: BacktestMetricsDto | null
  dailyResults: BacktestDailyResultDto[]
  failureMessage?: string
}

export interface StrategyApi {
  getDraft(strategyId: string): Promise<StrategyDraft>
  saveDraft(strategyId: string, input: DraftSaveInput): Promise<StrategyDraft>
  listRevisions(strategyId: string): Promise<DraftRevision[]>
  restoreRevision(
    strategyId: string,
    revisionId: string,
    reason: string,
    idempotencyKey: string,
  ): Promise<StrategyDraft>
  validateDraft(strategyId: string, reason: string): Promise<StrategyRunResult>
  testDraft(strategyId: string, reason: string): Promise<StrategyRunResult>
  publishDraft(strategyId: string, reason: string): Promise<StrategyRunResult>
  archiveStrategy(strategyId: string, reason: string): Promise<StrategyRunResult>
  listVersions(strategyId: string): Promise<StrategyVersion[]>
  createHoldoutBacktest(input: HoldoutBacktestInput): Promise<HoldoutBacktestResult>
  getHoldoutBacktest(backtestId: string): Promise<HoldoutBacktestResult>
}

export function isSaveConflict(error: unknown): error is SaveConflict {
  if (!error || typeof error !== "object") return false
  const candidate = error as Partial<SaveConflict>
  return candidate.status === 409 && candidate.current !== undefined
}
