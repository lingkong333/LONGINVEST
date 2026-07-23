import { z } from "zod"

import type {
  CalendarDay,
  CalendarGateway,
  CalendarImportFile,
  CalendarSnapshot,
  CalendarVersion,
} from "@/features/calendar/types"
import {
  ApiError,
  createApiClient,
  createClientRequestId,
} from "@/shared/api/client"
import type { paths } from "@/shared/api/generated/schema"

const actionSchema = z.enum(["IMPORT", "OVERRIDE", "RESTORE"])
const statusSchema = z.enum([
  "CONFIRMED",
  "PROVISIONAL",
  "OVERRIDDEN",
  "MISSING",
])
const sessionSchema = z.object({
  starts_at: z.string(),
  ends_at: z.string(),
})
const daySchema = z.object({
  trade_date: z.string(),
  is_trading_day: z.boolean(),
  status: statusSchema,
  source: z.string(),
  note: z.string().nullable(),
  override_reason: z.string().nullable(),
  sessions: z.array(sessionSchema),
  allowed_actions: z.array(actionSchema),
})
const dayListSchema = z.object({
  items: z.array(daySchema),
  allowed_actions: z.array(actionSchema),
})
const coverageSchema = z.object({
  market: z.string(),
  from_date: z.string(),
  confirmed_through: z.string().nullable(),
  future_confirmed_days: z.number().int().nonnegative(),
  level: z.string(),
  current_version_id: z.string().nullable(),
  missing_today: z.boolean(),
  allowed_actions: z.array(actionSchema),
})
const versionSchema = z.object({
  id: z.string(),
  market: z.string(),
  version_number: z.number().int().positive(),
  source: z.string(),
  source_version: z.string(),
  based_on_version_id: z.string().nullable(),
  reason: z.string().nullable(),
  created_at: z.string(),
  is_current: z.boolean(),
  allowed_actions: z.array(actionSchema),
})
const versionListSchema = z.object({
  items: z.array(versionSchema),
  allowed_actions: z.array(actionSchema),
})

export const calendarImportFileSchema = z.object({
  market: z.string().min(1).max(16),
  source: z.string().min(1).max(64),
  source_version: z.string().min(1).max(128),
  days: z.array(z.object({
    trade_date: z.string(),
    is_trading_day: z.boolean(),
    status: statusSchema,
    sessions: z.array(sessionSchema).optional(),
    note: z.string().nullable().optional(),
  }).strict()).min(1),
}).strict()

function parse<T>(schema: z.ZodType<T>, value: unknown, code: string): T {
  const result = schema.safeParse(value)
  if (!result.success) {
    throw new ApiError("交易日历接口返回的数据无法识别。", {
      code,
      cause: result.error,
    })
  }
  return result.data
}

function dayFrom(value: z.infer<typeof daySchema>): CalendarDay {
  return {
    tradeDate: value.trade_date,
    isTradingDay: value.is_trading_day,
    status: value.status,
    source: value.source,
    note: value.note,
    overrideReason: value.override_reason,
    sessions: value.sessions.map((session) => ({
      startsAt: session.starts_at,
      endsAt: session.ends_at,
    })),
    allowedActions: value.allowed_actions,
  }
}

function versionFrom(value: z.infer<typeof versionSchema>): CalendarVersion {
  return {
    id: value.id,
    market: value.market,
    versionNumber: value.version_number,
    source: value.source,
    sourceVersion: value.source_version,
    basedOnVersionId: value.based_on_version_id,
    reason: value.reason,
    createdAt: value.created_at,
    isCurrent: value.is_current,
    allowedActions: value.allowed_actions,
  }
}

export function createCalendarGateway(baseUrl = ""): CalendarGateway {
  const api = createApiClient<paths>({ baseUrl })

  return {
    async loadSnapshot(fromDate, throughDate): Promise<CalendarSnapshot> {
      const [daysValue, coverageValue, versionsValue] = await Promise.all([
        api.request<unknown>(api.client.GET("/api/v1/trading-calendar", {
          params: { query: { from: fromDate, through: throughDate, market: "CN_A" } },
        })),
        api.request<unknown>(api.client.GET("/api/v1/trading-calendar/coverage", {
          params: { query: { from: fromDate, market: "CN_A" } },
        })),
        api.request<unknown>(api.client.GET("/api/v1/trading-calendar/versions", {
          params: { query: { market: "CN_A" } },
        })),
      ])
      const days = parse(dayListSchema, daysValue, "INVALID_CALENDAR_DAYS")
      const coverage = parse(
        coverageSchema,
        coverageValue,
        "INVALID_CALENDAR_COVERAGE",
      )
      const versions = parse(
        versionListSchema,
        versionsValue,
        "INVALID_CALENDAR_VERSIONS",
      )
      return {
        days: days.items.map(dayFrom),
        coverage: {
          market: coverage.market,
          fromDate: coverage.from_date,
          confirmedThrough: coverage.confirmed_through,
          futureConfirmedDays: coverage.future_confirmed_days,
          level: coverage.level,
          currentVersionId: coverage.current_version_id,
          missingToday: coverage.missing_today,
          allowedActions: coverage.allowed_actions,
        },
        versions: versions.items.map(versionFrom),
        allowedActions: Array.from(new Set([
          ...days.allowed_actions,
          ...coverage.allowed_actions,
          ...versions.allowed_actions,
        ])),
      }
    },

    async overrideDay(input) {
      await api.request(api.client.PATCH("/api/v1/trading-calendar/{date}", {
        params: {
          path: { date: input.day.tradeDate },
          header: { "Idempotency-Key": createClientRequestId() },
        },
        body: {
          market: "CN_A",
          is_trading_day: input.isTradingDay,
          expected_current_version: input.expectedCurrentVersion,
          reason: input.reason,
          confirm: true,
          note: input.note || null,
        },
      }))
    },

    async importCalendar(input) {
      await api.request(api.client.POST("/api/v1/trading-calendar/import", {
        body: {
          market: input.file.market,
          source: input.file.source,
          source_version: input.file.source_version,
          days: input.file.days,
          expected_current_version: input.expectedCurrentVersion,
          reason: input.reason,
          confirm: true,
        },
        params: {
          header: { "Idempotency-Key": createClientRequestId() },
        },
      }))
    },

    async restoreVersion(input) {
      await api.request(api.client.POST(
        "/api/v1/trading-calendar/versions/{version_id}/restore",
        {
          params: {
            path: { version_id: input.version.id },
            header: { "Idempotency-Key": createClientRequestId() },
          },
          body: {
            market: "CN_A",
            expected_current_version: input.expectedCurrentVersion,
            reason: input.reason,
            confirm: true,
          },
        },
      ))
    },
  }
}

export function parseCalendarImportFile(value: unknown): CalendarImportFile {
  const parsed = calendarImportFileSchema.safeParse(value)
  if (!parsed.success) {
    throw new ApiError("文件内容不是有效的交易日历格式。", {
      code: "CALENDAR_IMPORT_FILE_INVALID",
      cause: parsed.error,
    })
  }
  return {
    ...parsed.data,
    days: parsed.data.days.map((day) => ({
      ...day,
      sessions: day.sessions ?? (day.is_trading_day
        ? [
            { starts_at: "09:30:00", ends_at: "11:30:00" },
            { starts_at: "13:00:00", ends_at: "15:00:00" },
          ]
        : []),
    })),
  }
}
