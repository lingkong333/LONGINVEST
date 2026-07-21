export type StrategyAction = "validate" | "test" | "publish" | "archive"

export interface StrategyRunResult {
  status: "SUCCEEDED" | "FAILED" | "RUNNING"
  sourceVersion: number
  summary: string
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

export type HoldoutBacktestStatus =
  | "QUEUED"
  | "RUNNING"
  | "PAUSED"
  | "PARTIAL_SUCCESS"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELED"
  | "TIMED_OUT"
  | "OFFLINE"

export interface HoldoutBacktestResult {
  id: string
  status: HoldoutBacktestStatus
  frozenTargets: Array<{ label: string; price: string }>
  adjustments: Array<{ eventDate: string; factor: string; source: string }>
  trades: Array<{ date: string; direction: "BUY" | "SELL"; price: string; quantity: string }>
  metrics: Array<{ label: string; value: string }>
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
