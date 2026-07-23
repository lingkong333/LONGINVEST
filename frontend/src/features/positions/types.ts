export type PositionStatus = "HOLDING" | "NOT_HOLDING"
export type PositionAction = "HOLD" | "CLEAR"

export interface PositionItem {
  securityId: string
  symbol: string
  securityName: string | null
  status: PositionStatus
  version: number
  source: string | null
  updatedAt: string | null
  isMonitored: boolean | null
  allowedActions: PositionAction[]
  warningCodes: string[]
}

export interface PositionHistoryItem {
  id: string
  symbol: string
  beforeStatus: PositionStatus | null
  afterStatus: PositionStatus
  version: number
  note: string | null
  source: string
  requestId: string
  effectiveAt: string
}

export interface PositionOverview {
  items: PositionItem[]
  warningCodes: string[]
}

export interface PositionBatchResult {
  symbol: string
  status: string
  code: string
}

export interface PositionGateway {
  loadCurrent(): Promise<PositionOverview>
  loadHistory(): Promise<PositionHistoryItem[]>
  changePosition(input: {
    symbol: string
    action: PositionAction
    expectedVersion: number
    reason: string
    note: string | null
  }): Promise<void>
  changeBatch(input: {
    items: Array<{
      symbol: string
      action: PositionAction
      expectedVersion: number
    }>
    reason: string
    note: string | null
  }): Promise<PositionBatchResult[]>
}
