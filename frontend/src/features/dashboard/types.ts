export type DashboardStatus = "HEALTHY" | "DEGRADED" | "UNHEALTHY"

export type DashboardSectionStatus =
  | "OK"
  | "EMPTY"
  | "WAITING"
  | "NON_TRADING_DAY"
  | "DEGRADED"
  | "ERROR"
  | "TIMEOUT"

export interface DashboardSection {
  status: DashboardSectionStatus
  updated_at: string
  data: Record<string, unknown>
  error: string | null
}

export interface DashboardSummary {
  status: DashboardStatus
  generated_at: string
  sections: {
    system: DashboardSection
    quote_batches: DashboardSection
    monitoring: DashboardSection
    positions: DashboardSection
    signals: DashboardSection
    daily_data: DashboardSection
    targets: DashboardSection
    jobs: DashboardSection
    notifications: DashboardSection
    providers: DashboardSection
    infrastructure: DashboardSection
    alerts: DashboardSection
  }
}

export interface DashboardGateway {
  loadSummary(): Promise<DashboardSummary>
}
