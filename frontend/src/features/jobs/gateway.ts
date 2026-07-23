import { z } from "zod"

import {
  jobItemStatuses,
  jobStatuses,
  type JobAction,
  type JobDetail,
  type JobGateway,
  type JobItem,
  type JobRun,
  type JobSummary,
  type Pagination,
} from "@/features/jobs/types"
import {
  ApiError,
  createApiClient,
  createClientIdempotencyKey,
} from "@/shared/api/client"
import type { paths } from "@/shared/api/generated/schema"

const jobStatusSchema = z.enum(jobStatuses)
const itemStatusSchema = z.enum(jobItemStatuses)
const actionSchema = z.enum([
  "cancel",
  "pause",
  "resume",
  "retry",
  "retry-failed-items",
])
const nullableRecordSchema = z.record(z.string(), z.unknown()).nullable()

const jobSchema = z.object({
  id: z.string(),
  job_type: z.string(),
  business_object_type: z.string().nullable(),
  business_object_id: z.string().nullable(),
  queue: z.string(),
  priority: z.number().int(),
  status: jobStatusSchema,
  progress: nullableRecordSchema,
  result_summary: nullableRecordSchema,
  current_run_id: z.string().nullable(),
  version: z.number().int().positive(),
  created_at: z.string(),
  updated_at: z.string(),
  terminal_at: z.string().nullable(),
})

const detailSchema = jobSchema.extend({
  config_snapshot: z.record(z.string(), z.unknown()),
  request_id: z.string(),
  created_by_user_id: z.string().nullable(),
  soft_timeout_seconds: z.number().int().nonnegative(),
  hard_timeout_seconds: z.number().int().nonnegative(),
})

const runSchema = z.object({
  id: z.string(),
  job_id: z.string(),
  attempt_no: z.number().int().positive(),
  worker_id: z.string().nullable(),
  status: z.string(),
  claimed_at: z.string().nullable(),
  started_at: z.string().nullable(),
  ended_at: z.string().nullable(),
  heartbeat_at: z.string().nullable(),
  exit_type: z.string().nullable(),
  error_code: z.string().nullable(),
  error_summary: z.string().nullable(),
  metrics: nullableRecordSchema,
})

const itemSchema = z.object({
  id: z.string(),
  job_id: z.string(),
  item_key: z.string(),
  status: itemStatusSchema,
  attempt_count: z.number().int().nonnegative(),
  result_ref: z.string().nullable(),
  error_code: z.string().nullable(),
  created_at: z.string(),
  started_at: z.string().nullable(),
  ended_at: z.string().nullable(),
  updated_at: z.string(),
})

const paginationSchema = z.object({
  page: z.number().int().positive(),
  page_size: z.number().int().positive(),
  total: z.number().int().nonnegative(),
})

const jobPageSchema = z.object({
  items: z.array(jobSchema),
  pagination: paginationSchema,
})
const runsSchema = z.object({ items: z.array(runSchema) })
const itemPageSchema = z.object({
  items: z.array(itemSchema),
  pagination: paginationSchema,
})
const actionsSchema = z.object({
  job_id: z.string(),
  allowed_actions: z.array(actionSchema),
})

function parse<T>(schema: z.ZodType<T>, value: unknown, code: string): T {
  const result = schema.safeParse(value)
  if (!result.success) {
    throw new ApiError("任务接口返回的数据无法识别。", {
      code,
      cause: result.error,
    })
  }
  return result.data
}

function paginationFrom(value: z.infer<typeof paginationSchema>): Pagination {
  return {
    page: value.page,
    pageSize: value.page_size,
    total: value.total,
  }
}

function summaryFrom(value: z.infer<typeof jobSchema>): JobSummary {
  return {
    id: value.id,
    jobType: value.job_type,
    businessObjectType: value.business_object_type,
    businessObjectId: value.business_object_id,
    queue: value.queue,
    priority: value.priority,
    status: value.status,
    progress: value.progress,
    resultSummary: value.result_summary,
    currentRunId: value.current_run_id,
    version: value.version,
    createdAt: value.created_at,
    updatedAt: value.updated_at,
    terminalAt: value.terminal_at,
  }
}

function detailFrom(value: z.infer<typeof detailSchema>): JobDetail {
  return {
    ...summaryFrom(value),
    configSnapshot: value.config_snapshot,
    requestId: value.request_id,
    createdByUserId: value.created_by_user_id,
    softTimeoutSeconds: value.soft_timeout_seconds,
    hardTimeoutSeconds: value.hard_timeout_seconds,
  }
}

function runFrom(value: z.infer<typeof runSchema>): JobRun {
  return {
    id: value.id,
    jobId: value.job_id,
    attemptNo: value.attempt_no,
    workerId: value.worker_id,
    status: value.status,
    claimedAt: value.claimed_at,
    startedAt: value.started_at,
    endedAt: value.ended_at,
    heartbeatAt: value.heartbeat_at,
    exitType: value.exit_type,
    errorCode: value.error_code,
    errorSummary: value.error_summary,
    metrics: value.metrics,
  }
}

function itemFrom(value: z.infer<typeof itemSchema>): JobItem {
  return {
    id: value.id,
    jobId: value.job_id,
    itemKey: value.item_key,
    status: value.status,
    attemptCount: value.attempt_count,
    resultRef: value.result_ref,
    errorCode: value.error_code,
    createdAt: value.created_at,
    startedAt: value.started_at,
    endedAt: value.ended_at,
    updatedAt: value.updated_at,
  }
}

function actionPath(action: JobAction) {
  return `/api/v1/jobs/{job_id}/${action}` as
    | "/api/v1/jobs/{job_id}/cancel"
    | "/api/v1/jobs/{job_id}/pause"
    | "/api/v1/jobs/{job_id}/resume"
    | "/api/v1/jobs/{job_id}/retry"
    | "/api/v1/jobs/{job_id}/retry-failed-items"
}

export function createJobGateway(baseUrl = ""): JobGateway {
  const api = createApiClient<paths>({ baseUrl })

  return {
    async loadJobs(filters) {
      const value = parse(
        jobPageSchema,
        await api.request<unknown>(api.client.GET("/api/v1/jobs", {
          params: {
            query: {
              page: filters.page,
              page_size: filters.pageSize,
              status: filters.status,
              job_type: filters.jobType || undefined,
              queue: filters.queue || undefined,
              created_from: filters.createdFrom || undefined,
              created_to: filters.createdTo || undefined,
            },
          },
        })),
        "INVALID_JOB_LIST",
      )
      return {
        items: value.items.map(summaryFrom),
        pagination: paginationFrom(value.pagination),
      }
    },

    async loadDetails(jobId) {
      const [detailValue, runsValue, itemsValue, actionsValue] =
        await Promise.all([
          api.request<unknown>(api.client.GET("/api/v1/jobs/{job_id}", {
            params: { path: { job_id: jobId } },
          })),
          api.request<unknown>(api.client.GET("/api/v1/jobs/{job_id}/runs", {
            params: { path: { job_id: jobId } },
          })),
          api.request<unknown>(api.client.GET("/api/v1/jobs/{job_id}/items", {
            params: {
              path: { job_id: jobId },
              query: { page: 1, page_size: 100 },
            },
          })),
          api.request<unknown>(
            api.client.GET("/api/v1/jobs/{job_id}/allowed-actions", {
              params: { path: { job_id: jobId } },
            }),
          ),
        ])
      const detail = parse(detailSchema, detailValue, "INVALID_JOB_DETAIL")
      const runs = parse(runsSchema, runsValue, "INVALID_JOB_RUNS")
      const items = parse(itemPageSchema, itemsValue, "INVALID_JOB_ITEMS")
      const actions = parse(
        actionsSchema,
        actionsValue,
        "INVALID_JOB_ACTIONS",
      )
      return {
        job: detailFrom(detail),
        runs: runs.items.map(runFrom),
        items: items.items.map(itemFrom),
        itemPagination: paginationFrom(items.pagination),
        allowedActions: actions.allowed_actions,
      }
    },

    async runAction(input) {
      await api.request(api.client.POST(actionPath(input.action), {
        params: {
          path: { job_id: input.jobId },
          header: { "Idempotency-Key": createClientIdempotencyKey() },
        },
        body: {
          confirm: true,
          reason: input.reason,
          expected_version: input.expectedVersion,
        },
      }))
    },
  }
}

export const jobGateway = createJobGateway()
