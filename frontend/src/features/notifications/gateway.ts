import { z } from "zod"

import type {
  NotificationAction,
  NotificationGateway,
  NotificationPolicy,
  PolicyScope,
} from "@/features/notifications/types"
import {
  ApiError,
  createApiClient,
  createClientRequestId,
} from "@/shared/api/client"
import type { paths } from "@/shared/api/generated/schema"

const actionSchema = z.enum([
  "RETRY",
  "CANCEL",
  "UPDATE",
  "TEST",
  "PROBE",
  "RESET_CIRCUIT",
  "PREVIEW",
  "ACTIVATE",
])
const channelSchema = z.enum(["WECOM", "EMAIL"])
const actionsSchema = z.array(actionSchema).optional().default([])

const pageFields = {
  page: z.number().int().positive(),
  page_size: z.number().int().positive(),
  total: z.number().int().nonnegative(),
}

const eventPageSchema = z.object({
  items: z.array(z.object({
    id: z.string(),
    event_type: z.string(),
    business_event_type: z.string(),
    business_object_type: z.string(),
    business_object_id: z.string(),
    severity: z.string().nullable(),
    status: z.string(),
    eligibility_status: z.string(),
    suppression_reason: z.string().nullable(),
    effective_channels: z.array(channelSchema),
    template_version: z.string(),
    created_at: z.string(),
    allowed_actions: actionsSchema,
  })),
  ...pageFields,
})

const deliveryPageSchema = z.object({
  items: z.array(z.object({
    id: z.string(),
    event_id: z.string(),
    generation: z.number().int().positive(),
    channel: channelSchema,
    target_fingerprint: z.string(),
    status: z.string(),
    attempt_count: z.number().int().nonnegative(),
    next_retry_at: z.string().nullable(),
    sent_at: z.string().nullable(),
    error_code: z.string().nullable(),
    created_at: z.string(),
    updated_at: z.string(),
    allowed_actions: actionsSchema,
    requires_duplicate_confirmation: z.boolean().optional().default(false),
  })),
  ...pageFields,
})

const attemptPageSchema = z.object({
  items: z.array(z.object({
    id: z.string(),
    delivery_id: z.string(),
    attempt_no: z.number().int().positive(),
    phase: z.string(),
    duration_ms: z.number().int().nonnegative().nullable(),
    outcome: z.string(),
    possibly_delivered: z.boolean(),
    error_code: z.string().nullable(),
    response_summary: z.string().nullable(),
    started_at: z.string(),
    finished_at: z.string().nullable(),
  })),
  ...pageFields,
})

const settingSchema = z.object({
  key: z.string(),
  value: z.record(z.string(), z.unknown()),
  version: z.number().int().positive(),
  allowed_actions: actionsSchema,
})

const secretSchema = z.object({
  key: z.string(),
  configured: z.boolean(),
  fingerprint: z.string().nullable().optional(),
})

const channelsSchema = z.object({
  channels: z.array(settingSchema),
  secrets: z.array(secretSchema),
  allowed_actions: actionsSchema,
})

const policySchema = settingSchema

const templateListSchema = z.object({
  items: z.array(z.object({
    template_type: z.string(),
    version: z.string(),
    active: z.boolean(),
    created_at: z.string(),
    allowed_actions: actionsSchema,
  })),
  allowed_actions: actionsSchema,
})

const previewSchema = z.object({
  subject: z.string().nullable().optional(),
  text: z.string(),
  html: z.string().nullable().optional(),
})

function parse<T>(schema: z.ZodType<T>, value: unknown, code: string): T {
  const parsed = schema.safeParse(value)
  if (!parsed.success) {
    throw new ApiError("通知接口返回的数据无法识别。", {
      code,
      cause: parsed.error,
    })
  }
  return parsed.data
}

function actionsOf(
  itemActions: NotificationAction[],
  containerActions: NotificationAction[],
) {
  return itemActions.length > 0 ? itemActions : containerActions
}

function channelsOf(value: unknown) {
  return z.array(channelSchema).catch([]).parse(value)
}

function policyFrom(
  scope: PolicyScope,
  value: z.infer<typeof policySchema>,
): NotificationPolicy {
  const setting = value.value
  return {
    scope,
    enabled: setting.enabled === true,
    channels: channelsOf(setting.channels),
    warning: channelsOf(setting.warning),
    error: channelsOf(setting.error),
    critical: channelsOf(setting.critical),
    recovered: channelsOf(setting.recovered),
    dailyUnresolved: channelsOf(setting.daily_unresolved),
    version: value.version,
    allowedActions: value.allowed_actions,
  }
}

function policyValue(policy: NotificationPolicy) {
  if (policy.scope !== "system-alerts") {
    return { enabled: policy.enabled, channels: policy.channels }
  }
  return {
    enabled: policy.enabled,
    warning: policy.warning,
    error: policy.error,
    critical: policy.critical,
    recovered: policy.recovered,
    daily_unresolved: policy.dailyUnresolved,
  }
}

export function createNotificationGateway(baseUrl = ""): NotificationGateway {
  const api = createApiClient<paths>({ baseUrl })

  return {
    async loadEvents() {
      const value = parse(
        eventPageSchema,
        await api.request<unknown>(api.client.GET("/api/v1/notifications/events", {
          params: { query: { page: 1, page_size: 100 } },
        })),
        "INVALID_NOTIFICATION_EVENTS",
      )
      return {
        items: value.items.map((item) => ({
          id: item.id,
          eventType: item.event_type,
          businessEventType: item.business_event_type,
          businessObjectType: item.business_object_type,
          businessObjectId: item.business_object_id,
          severity: item.severity ?? "INFO",
          status: item.status,
          eligibilityStatus: item.eligibility_status,
          suppressionReason: item.suppression_reason,
          effectiveChannels: item.effective_channels,
          templateVersion: item.template_version,
          createdAt: item.created_at,
          allowedActions: item.allowed_actions,
        })),
        page: value.page,
        pageSize: value.page_size,
        total: value.total,
      }
    },

    async loadDeliveries() {
      const value = parse(
        deliveryPageSchema,
        await api.request<unknown>(api.client.GET("/api/v1/notifications/deliveries", {
          params: { query: { page: 1, page_size: 100 } },
        })),
        "INVALID_NOTIFICATION_DELIVERIES",
      )
      return {
        items: value.items.map((item) => ({
          id: item.id,
          eventId: item.event_id,
          generation: item.generation,
          channel: item.channel,
          targetFingerprint: item.target_fingerprint,
          status: item.status,
          attemptCount: item.attempt_count,
          nextRetryAt: item.next_retry_at,
          sentAt: item.sent_at,
          errorCode: item.error_code,
          createdAt: item.created_at,
          updatedAt: item.updated_at,
          allowedActions: item.allowed_actions,
          requiresDuplicateConfirmation: item.requires_duplicate_confirmation,
        })),
        page: value.page,
        pageSize: value.page_size,
        total: value.total,
      }
    },

    async loadAttempts(deliveryId) {
      const value = parse(
        attemptPageSchema,
        await api.request<unknown>(api.client.GET(
          "/api/v1/notifications/deliveries/{delivery_id}/attempts",
          {
            params: {
              path: { delivery_id: deliveryId },
              query: { page: 1, page_size: 100 },
            },
          },
        )),
        "INVALID_NOTIFICATION_ATTEMPTS",
      )
      return {
        items: value.items.map((item) => ({
          id: item.id,
          deliveryId: item.delivery_id,
          attemptNo: item.attempt_no,
          phase: item.phase,
          durationMs: item.duration_ms,
          outcome: item.outcome,
          possiblyDelivered: item.possibly_delivered,
          errorCode: item.error_code,
          responseSummary: item.response_summary,
          startedAt: item.started_at,
          finishedAt: item.finished_at,
        })),
        page: value.page,
        pageSize: value.page_size,
        total: value.total,
      }
    },

    async retryDelivery(input) {
      await api.request(api.client.POST(
        "/api/v1/notifications/deliveries/{delivery_id}/retry",
        {
          params: {
            path: { delivery_id: input.deliveryId },
            header: { "Idempotency-Key": createClientRequestId() },
          },
          body: {
            reason: input.reason,
            confirm: true,
            confirm_duplicate_risk: input.confirmDuplicateRisk,
          },
        },
      ))
    },

    async cancelDelivery(deliveryId, reason) {
      await api.request(api.client.POST(
        "/api/v1/notifications/deliveries/{delivery_id}/cancel",
        {
          params: {
            path: { delivery_id: deliveryId },
            header: { "Idempotency-Key": createClientRequestId() },
          },
          body: { reason, confirm: true },
        },
      ))
    },

    async loadChannels() {
      const value = parse(
        channelsSchema,
        await api.request<unknown>(
          api.client.GET("/api/v1/notifications/channels"),
        ),
        "INVALID_NOTIFICATION_CHANNELS",
      )
      const secretByKey = new Map(value.secrets.map((item) => [item.key, item]))
      return (["WECOM", "EMAIL"] as const).map((channel) => {
        const key = channel === "WECOM"
          ? "notification.channel.wecom"
          : "notification.channel.email"
        const secretKey = channel === "WECOM"
          ? "notification.wecom.webhook"
          : "notification.email.password"
        const setting = value.channels.find((item) => item.key === key)
        const config = setting?.value ?? {}
        const secret = secretByKey.get(secretKey)
        return {
          channel,
          enabled: config.enabled === true,
          timeoutSeconds: typeof config.timeout_seconds === "number"
            ? config.timeout_seconds
            : 0,
          smtpHost: typeof config.smtp_host === "string" ? config.smtp_host : null,
          smtpPort: typeof config.smtp_port === "number" ? config.smtp_port : null,
          security: typeof config.security === "string" ? config.security : null,
          sender: typeof config.sender === "string" ? config.sender : null,
          recipients: z.array(z.string()).catch([]).parse(config.recipients),
          version: setting?.version ?? 0,
          secretConfigured: secret?.configured ?? false,
          secretFingerprint: secret?.fingerprint ?? null,
          allowedActions: actionsOf(
            setting?.allowed_actions ?? [],
            value.allowed_actions,
          ),
        }
      })
    },

    async runChannelAction(input) {
      const params = {
        params: {
          path: { channel: input.channel },
          header: { "Idempotency-Key": createClientRequestId() },
        },
        body: {
          reason: input.reason,
          confirm: true as const,
          message: input.message,
        },
      }
      if (input.action === "TEST") {
        await api.request(api.client.POST(
          "/api/v1/notifications/channels/{channel}/test",
          params,
        ))
      } else if (input.action === "PROBE") {
        await api.request(api.client.POST(
          "/api/v1/notification-channels/{channel}/probe",
          params,
        ))
      } else {
        await api.request(api.client.POST(
          "/api/v1/notification-channels/{channel}/reset-circuit",
          {
            params: params.params,
            body: { reason: input.reason, confirm: true },
          },
        ))
      }
    },

    async loadPolicy(scope) {
      const value = parse(
        policySchema,
        await api.request<unknown>(api.client.GET(
          "/api/v1/notifications/policies/{scope}",
          { params: { path: { scope } } },
        )),
        "INVALID_NOTIFICATION_POLICY",
      )
      return policyFrom(scope, value)
    },

    async updatePolicy(policy, reason) {
      await api.request(api.client.PATCH(
        "/api/v1/notifications/policies/{scope}",
        {
          params: {
            path: { scope: policy.scope },
            header: { "Idempotency-Key": createClientRequestId() },
          },
          body: {
            value: policyValue(policy),
            expected_version: policy.version,
            reason,
            confirm: true,
          },
        },
      ))
    },

    async loadTemplates() {
      const value = parse(
        templateListSchema,
        await api.request<unknown>(
          api.client.GET("/api/v1/notifications/templates"),
        ),
        "INVALID_NOTIFICATION_TEMPLATES",
      )
      return value.items.map((item) => ({
        templateType: item.template_type,
        version: item.version,
        active: item.active,
        createdAt: item.created_at,
        allowedActions: actionsOf(item.allowed_actions, value.allowed_actions),
      }))
    },

    async previewTemplate(input) {
      const value = parse(
        previewSchema,
        await api.request<unknown>(api.client.POST(
          "/api/v1/notification-templates/{type}/preview",
          {
            params: { path: { type: input.templateType } },
            body: {
              version: input.version,
              variables: input.variables,
              test_message: true,
            },
          },
        )),
        "INVALID_NOTIFICATION_TEMPLATE_PREVIEW",
      )
      return {
        subject: value.subject ?? null,
        text: value.text,
        html: value.html ?? null,
      }
    },

    async activateTemplate(template, reason) {
      await api.request(api.client.POST(
        "/api/v1/notification-templates/{type}/activate",
        {
          params: {
            path: { type: template.templateType },
            header: { "Idempotency-Key": createClientRequestId() },
          },
          body: {
            version: template.version,
            reason,
            confirm: true,
          },
        },
      ))
    },
  }
}

export const notificationGateway = createNotificationGateway()
