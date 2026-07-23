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
  warningCodes: string[]
}

export interface MonitoringOverview {
  generatedAt: string
  items: MonitoringOverviewItem[]
  warningCodes: string[]
}

export interface MonitoringGateway {
  loadOverview(): Promise<MonitoringOverview>
}
