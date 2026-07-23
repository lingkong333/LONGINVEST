export type ProviderCode = "EASTMONEY" | "SINA"
export type ProviderAction =
  | "UPDATE_SETTINGS"
  | "PROBE"
  | "RESET"
  | "QUOTE_DIAGNOSTICS"
export type CircuitState = "CLOSED" | "OPEN" | "HALF_OPEN" | "DISABLED"

export interface ProviderCapability {
  capability: string
  enabled: boolean
  priority: number
  concurrency: number
  ratePerSecond: number
  timeoutSeconds: number
  autoSwitch: boolean
}

export interface ProviderSummary {
  code: ProviderCode
  version: number
  reason: string
  capabilities: ProviderCapability[]
  allowedActions: ProviderAction[]
}

export interface ProviderHealth {
  capability: string
  status: string
  consecutiveFailures: number
  lastSuccessAt: string | null
  lastFailureAt: string | null
  successRate: number | null
  p95LatencyMs: number | null
  rateLimitWaitMs: number | null
  switchCount: number | null
  schemaErrors: number | null
}

export interface ProviderCircuit {
  id: string
  providerCode: ProviderCode
  capability: string
  state: CircuitState
  consecutiveFailures: number
  cooldownIndex: number
  openedAt: string | null
  allowedActions: ProviderAction[]
}

export interface ProviderSettingsInput {
  provider: ProviderSummary
  settings: Omit<ProviderCapability, "capability">
  reason: string
}

export interface QuoteDiagnostic {
  symbols: string[]
  sources: {
    provider: ProviderCode
    items: { symbol: string; price: string; quoteTime: string }[]
    failures: { symbol: string; code: string }[]
    batchErrorCode: string | null
  }[]
  comparisons: {
    symbol: string
    status: "MATCH" | "CONFLICT" | "INCOMPLETE"
    missingSources: ProviderCode[]
  }[]
}

export interface ProviderGateway {
  loadProviders(): Promise<ProviderSummary[]>
  loadHealth(providerCode: ProviderCode): Promise<ProviderHealth[]>
  loadCircuits(): Promise<ProviderCircuit[]>
  updateSettings(input: ProviderSettingsInput): Promise<void>
  runCircuitAction(input: {
    circuit: ProviderCircuit
    action: "PROBE" | "RESET"
    reason: string
  }): Promise<void>
  runQuoteDiagnostics(symbols: string[], reason: string): Promise<QuoteDiagnostic>
}
