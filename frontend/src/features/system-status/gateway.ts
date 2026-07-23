import { z } from "zod"

import type {
  ComponentStatus,
  OccurrencePage,
  OverallHealth,
  RuntimeStatus,
  SchedulingStatus,
  SystemStatusGateway,
} from "@/features/system-status/types"
import { ApiError, createApiClient } from "@/shared/api/client"
import type { paths } from "@/shared/api/generated/schema"

const healthStatusSchema = z.enum([
  "HEALTHY",
  "DEGRADED",
  "UNAVAILABLE",
  "UNKNOWN",
])
const emptyActionsSchema = z.array(z.never()).length(0)
const detailSchema = z.object({
  key: z.string().min(1),
  value: z.union([z.string(), z.number(), z.boolean(), z.null()]),
  unit: z.string().nullable(),
})
const componentSchema = z.object({
  name: z.string().min(1),
  category: z.string().min(1),
  status: healthStatusSchema,
  critical: z.boolean(),
  source: z.string().min(1),
  updated_at: z.string().min(1),
  message: z.string().nullable(),
  details: z.array(detailSchema),
})
const overallSchema = z.object({
  status: healthStatusSchema,
  updated_at: z.string().min(1),
  components: z.array(componentSchema),
  allowed_actions: emptyActionsSchema,
})
const workerSchema = z.object({
  worker_id: z.string().min(1),
  queue: z.string().min(1),
  status: z.string().min(1),
  current_job_id: z.string().uuid().nullable(),
  heartbeat_at: z.string().nullable(),
  processed_jobs: z.number().int().nonnegative(),
  failed_jobs: z.number().int().nonnegative(),
})
const queueSchema = z.object({
  name: z.string().min(1),
  status: healthStatusSchema,
  depth: z.number().int().nonnegative(),
  active_workers: z.number().int().nonnegative(),
  oldest_job_at: z.string().nullable(),
  updated_at: z.string().min(1),
})
const schedulerSchema = z.object({
  status: healthStatusSchema,
  scan_interval_seconds: z.number().int().positive(),
  last_scan_at: z.string().nullable(),
  database_time: z.string().nullable(),
  automatic_scheduling_paused: z.boolean(),
  pause_reason: z.string().nullable(),
  updated_at: z.string().min(1),
  allowed_actions: emptyActionsSchema,
})
const clockSourceSchema = z.object({
  source: z.string().min(1),
  observed_at: z.string().nullable(),
  skew_seconds: z.number().nullable(),
  status: healthStatusSchema,
})
const clockSchema = z.object({
  status: healthStatusSchema,
  application_time: z.string().min(1),
  database_time: z.string().nullable(),
  max_skew_seconds: z.number().nullable(),
  automatic_scheduling_paused: z.boolean(),
  sources: z.array(clockSourceSchema),
  updated_at: z.string().min(1),
  allowed_actions: emptyActionsSchema,
})
const occurrenceSchema = z.object({
  occurrence_id: z.string().uuid(),
  occurrence_type: z.string().min(1),
  definition_id: z.string().min(1),
  scheduled_trade_date: z.string().min(1),
  scheduled_at: z.string().min(1),
  status: z.string().min(1),
  job_id: z.string().uuid().nullable(),
  missed_reason: z.string().nullable(),
  created_at: z.string().min(1),
})
const paginationSchema = z.object({
  page: z.number().int().positive(),
  page_size: z.number().int().positive(),
  total: z.number().int().nonnegative(),
})

function parse<T>(schema: z.ZodType<T>, value: unknown, code: string): T {
  const parsed = schema.safeParse(value)
  if (!parsed.success) {
    throw new ApiError("运行状态接口返回的数据无法识别。", {
      code,
      cause: parsed.error,
    })
  }
  return parsed.data
}

function component(value: z.infer<typeof componentSchema>): ComponentStatus {
  return {
    name: value.name,
    category: value.category,
    status: value.status,
    critical: value.critical,
    source: value.source,
    updatedAt: value.updated_at,
    message: value.message,
    details: value.details.map((item) => ({
      key: item.key,
      value: String(item.value ?? "暂无"),
      unit: item.unit,
    })),
    allowedActions: [],
  }
}

export function createSystemStatusGateway(baseUrl = ""): SystemStatusGateway {
  const api = createApiClient<paths>({ baseUrl })
  return {
    async loadOverall() {
      const value = parse(
        overallSchema,
        await api.request<unknown>(api.client.GET("/api/v1/system/health")),
        "SYSTEM_HEALTH_INVALID",
      )
      return {
        status: value.status,
        updatedAt: value.updated_at,
        componentCount: value.components.length,
        unhealthyCount: value.components.filter(
          (item) => item.status !== "HEALTHY",
        ).length,
        allowedActions: value.allowed_actions,
      } satisfies OverallHealth
    },
    async loadComponents() {
      const value = parse(
        z.object({
          items: z.array(componentSchema),
          allowed_actions: emptyActionsSchema,
        }),
        await api.request<unknown>(api.client.GET("/api/v1/system/components")),
        "SYSTEM_COMPONENTS_INVALID",
      )
      return value.items.map(component)
    },
    async loadRuntime() {
      const [workerValue, queueValue] = await Promise.all([
        api.request<unknown>(api.client.GET("/api/v1/workers")),
        api.request<unknown>(api.client.GET("/api/v1/queues")),
      ])
      const workerResult = parse(
        z.object({
          items: z.array(workerSchema),
          allowed_actions: emptyActionsSchema,
        }),
        workerValue,
        "SYSTEM_WORKERS_INVALID",
      )
      const queueResult = parse(
        z.object({
          items: z.array(queueSchema),
          allowed_actions: emptyActionsSchema,
        }),
        queueValue,
        "SYSTEM_QUEUES_INVALID",
      )
      const workers = workerResult.items
      const queues = queueResult.items
      return {
        workers: workers.map((item) => ({
          workerId: item.worker_id,
          queue: item.queue,
          status: item.status,
          currentJobId: item.current_job_id,
          heartbeatAt: item.heartbeat_at,
          processedJobs: item.processed_jobs,
          failedJobs: item.failed_jobs,
        })),
        queues: queues.map((item) => ({
          name: item.name,
          status: item.status,
          depth: item.depth,
          activeWorkers: item.active_workers,
          oldestJobAt: item.oldest_job_at,
          updatedAt: item.updated_at,
        })),
        allowedActions: [
          ...workerResult.allowed_actions,
          ...queueResult.allowed_actions,
        ],
      } satisfies RuntimeStatus
    },
    async loadScheduling() {
      const [schedulerValue, clockValue] = await Promise.all([
        api.request<unknown>(api.client.GET("/api/v1/scheduler/status")),
        api.request<unknown>(api.client.GET("/api/v1/system-clock/status")),
      ])
      const scheduler = parse(
        schedulerSchema,
        schedulerValue,
        "SCHEDULER_STATUS_INVALID",
      )
      const clock = parse(clockSchema, clockValue, "SYSTEM_CLOCK_INVALID")
      return {
        scheduler: {
          status: scheduler.status,
          scanIntervalSeconds: scheduler.scan_interval_seconds,
          lastScanAt: scheduler.last_scan_at,
          databaseTime: scheduler.database_time,
          automaticSchedulingPaused: scheduler.automatic_scheduling_paused,
          pauseReason: scheduler.pause_reason,
          updatedAt: scheduler.updated_at,
        },
        clock: {
          status: clock.status,
          applicationTime: clock.application_time,
          databaseTime: clock.database_time,
          maxSkewSeconds: clock.max_skew_seconds,
          automaticSchedulingPaused: clock.automatic_scheduling_paused,
          sources: clock.sources.map((item) => ({
            source: item.source,
            observedAt: item.observed_at,
            skewSeconds: item.skew_seconds,
            status: item.status,
          })),
          updatedAt: clock.updated_at,
        },
        allowedActions: [
          ...scheduler.allowed_actions,
          ...clock.allowed_actions,
        ],
      } satisfies SchedulingStatus
    },
    async loadOccurrences() {
      const value = parse(
        z.object({
          items: z.array(occurrenceSchema),
          pagination: paginationSchema,
          allowed_actions: emptyActionsSchema,
        }),
        await api.request<unknown>(api.client.GET(
          "/api/v1/schedule-occurrences",
          { params: { query: { page: 1, page_size: 20 } } },
        )),
        "SCHEDULE_OCCURRENCES_INVALID",
      )
      return {
        items: value.items.map((item) => ({
          occurrenceId: item.occurrence_id,
          occurrenceType: item.occurrence_type,
          definitionId: item.definition_id,
          scheduledTradeDate: item.scheduled_trade_date,
          scheduledAt: item.scheduled_at,
          status: item.status,
          jobId: item.job_id,
          missedReason: item.missed_reason,
          createdAt: item.created_at,
          allowedActions: [],
        })),
        page: value.pagination.page,
        pageSize: value.pagination.page_size,
        total: value.pagination.total,
        allowedActions: value.allowed_actions,
      } satisfies OccurrencePage
    },
  }
}

export const systemStatusGateway = createSystemStatusGateway()
