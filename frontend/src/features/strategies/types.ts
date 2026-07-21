/**
 * Temporary feature-local API shapes. Task 7 replaces these with generated
 * OpenAPI types when the public Stage 4 endpoints are integrated.
 */
export interface StrategyDraft {
  id: string
  strategyId: string
  name: string
  description: string
  sourceCode: string
  parameterSchema: string
  version: number
  updatedAt: string
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

export interface HoldoutBacktestResult {
  id: string
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED"
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
  restoreRevision(strategyId: string, revisionId: string, reason: string): Promise<StrategyDraft>
  validateDraft(strategyId: string, reason: string): Promise<void>
  testDraft(strategyId: string, reason: string): Promise<void>
  publishDraft(strategyId: string, reason: string): Promise<void>
  archiveStrategy(strategyId: string, reason: string): Promise<void>
  listVersions(strategyId: string): Promise<StrategyVersion[]>
  createHoldoutBacktest(input: HoldoutBacktestInput): Promise<HoldoutBacktestResult>
  getHoldoutBacktest(backtestId: string): Promise<HoldoutBacktestResult>
}

export function isSaveConflict(error: unknown): error is SaveConflict {
  if (!error || typeof error !== "object") {
    return false
  }
  const candidate = error as Partial<SaveConflict>
  return candidate.status === 409 && candidate.current !== undefined
}
