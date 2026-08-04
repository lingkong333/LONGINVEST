export type MonitoringAction =
  | "ENABLE"
  | "DISABLE"
  | "ARCHIVE"
  | "RESTORE"
  | "CHECK_NOW"
  | "DIAGNOSE"

export interface MonitoringOverviewItem {
  subscriptionId: string
  symbol: string
  securityName: string | null
  groups: string[]
  isHolding: boolean
  subscriptionStatus: string
  subscriptionVersion: number
  scheduleName: string | null
  targetMode: string | null
  strategyVersionId: string | null
  targetStatus: string | null
  zone: string | null
  lastPrice: string | null
  lastPriceAt: string | null
  allowedActions: MonitoringAction[]
  warningCodes: string[]
}

export interface MonitoringOverview {
  generatedAt: string
  items: MonitoringOverviewItem[]
  warningCodes: string[]
}

export interface MonitorSchedule {
  id: string
  name: string
  version: number
  times: string[]
}

export interface MonitorScheduleInput {
  id?: string
  name: string
  version?: number
  times: string[]
  reason: string
}

export interface MonitoringGateway {
  loadOverview(): Promise<MonitoringOverview>
  loadSchedules(): Promise<MonitorSchedule[]>
  saveSchedule(input: MonitorScheduleInput): Promise<void>
  runAction(
    subscriptionId: string,
    action: MonitoringAction,
    expectedVersion: number,
    reason: string,
  ): Promise<void>
}
