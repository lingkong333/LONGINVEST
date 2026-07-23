export type CalendarAction = "IMPORT" | "OVERRIDE" | "RESTORE"
export type CalendarDayStatus =
  | "CONFIRMED"
  | "PROVISIONAL"
  | "OVERRIDDEN"
  | "MISSING"

export interface TradingSession {
  startsAt: string
  endsAt: string
}

export interface CalendarDay {
  tradeDate: string
  isTradingDay: boolean
  status: CalendarDayStatus
  source: string
  note: string | null
  overrideReason: string | null
  sessions: TradingSession[]
  allowedActions: CalendarAction[]
}

export interface CalendarCoverage {
  market: string
  fromDate: string
  confirmedThrough: string | null
  futureConfirmedDays: number
  level: string
  currentVersionId: string | null
  missingToday: boolean
  allowedActions: CalendarAction[]
}

export interface CalendarVersion {
  id: string
  market: string
  versionNumber: number
  source: string
  sourceVersion: string
  basedOnVersionId: string | null
  reason: string | null
  createdAt: string
  isCurrent: boolean
  allowedActions: CalendarAction[]
}

export interface CalendarSnapshot {
  days: CalendarDay[]
  coverage: CalendarCoverage
  versions: CalendarVersion[]
  allowedActions: CalendarAction[]
}

export interface CalendarDayInput {
  trade_date: string
  is_trading_day: boolean
  status: CalendarDayStatus
  sessions: { starts_at: string; ends_at: string }[]
  note?: string | null
}

export interface CalendarImportFile {
  market: string
  source: string
  source_version: string
  days: CalendarDayInput[]
}

export interface CalendarGateway {
  loadSnapshot(fromDate: string, throughDate: string): Promise<CalendarSnapshot>
  overrideDay(input: {
    day: CalendarDay
    isTradingDay: boolean
    expectedCurrentVersion: number
    reason: string
    note: string
  }): Promise<void>
  importCalendar(input: {
    file: CalendarImportFile
    expectedCurrentVersion: number | null
    reason: string
  }): Promise<void>
  restoreVersion(input: {
    version: CalendarVersion
    expectedCurrentVersion: number
    reason: string
  }): Promise<void>
}
