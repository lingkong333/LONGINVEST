export interface PageInfo {
  page: number
  pageSize: number
  total: number
}

export interface SecuritySummary {
  id: string
  symbol: string
  name: string
  market: string
  listingStatus: string
  isSt: boolean
  isSuspended: boolean
  masterVersion: number
  updatedAt: string
}

export interface QuoteCycleSummary {
  id: string
  status: string
  expectedCount: number
  validCount: number
  missingCount: number
  conflictCount: number
  failedCount: number
  scheduledAt: string
  finalizedAt: string | null
}

export interface QuoteItemSummary {
  id: string
  symbol: string
  status: string
  price: string | null
  provider: string | null
  quoteTime: string | null
  errorCode: string | null
  eligibleForEvaluation: boolean
}

export interface DailyBatchSummary {
  id: string
  tradingDate: string
  status: string
  expectedCount: number
  fetchedCount: number
  committedCount: number
  missingCount: number
  failedCount: number
  createdAt: string
  completedAt: string | null
}

export interface QfqDatasetSummary {
  id: string
  symbol: string
  version: number
  actualStart: string
  actualEnd: string
  asOfDate: string
  provider: string
  rowCount: number
  lifecycle: string
  freshness: string
  staleReason: string | null
  activatedAt: string | null
}

export interface QualityIssueSummary {
  id: string
  issueType: string
  subjectType: string
  symbol: string | null
  status: string
  severity: string
  occurrenceCount: number
  lastSeenAt: string
  selectedSource: string | null
  sourceCandidates: string[]
  allowedActions: QualityIssueAction[]
}

export type QualityIssueAction = "SELECT_SOURCE" | "INVALIDATE" | "REFETCH"

export interface QualityIssueCommand {
  issueId: string
  action: QualityIssueAction
  reason: string
  selectedSource?: string
}

export interface BackfillSummary {
  id: string
  status: string
  version: number
  completed: number
  total: number
  succeeded: number | null
  failed: number | null
  updatedAt: string
  terminalAt: string | null
}

export interface PagedResult<Item> {
  items: Item[]
  pagination: PageInfo
}

export interface MarketDataGateway {
  loadSecurities(): Promise<PagedResult<SecuritySummary>>
  loadQuoteCycles(): Promise<PagedResult<QuoteCycleSummary>>
  loadQuoteItems(cycleId: string): Promise<QuoteItemSummary[]>
  loadDailyBatches(): Promise<PagedResult<DailyBatchSummary>>
  loadQfq(symbol: string): Promise<QfqDatasetSummary>
  loadQualityIssues(): Promise<PagedResult<QualityIssueSummary>>
  runQualityAction(command: QualityIssueCommand): Promise<void>
  loadBackfills(): Promise<PagedResult<BackfillSummary>>
}
