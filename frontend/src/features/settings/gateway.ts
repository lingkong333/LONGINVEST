import { z } from "zod"

import { secretKeys, settingKeys } from "@/features/settings/types"
import type {
  SecretStatus,
  SettingHistoryItem,
  SettingItem,
  SettingsGateway,
} from "@/features/settings/types"
import { ApiError, createApiClient, createClientIdempotencyKey } from "@/shared/api/client"
import type { paths } from "@/shared/api/generated/schema"

const settingKeySchema = z.enum(settingKeys)
const secretKeySchema = z.enum(secretKeys)
const settingKeySet: ReadonlySet<string> = new Set(settingKeys)
const secretKeySet: ReadonlySet<string> = new Set(secretKeys)
const settingActionSchema = z.enum(["UPDATE", "ROLLBACK"])
const secretActionSchema = z.enum(["UPDATE", "CLEAR"])
const definitionSchema = z.object({
  value_type: z.string(),
  default_value: z.unknown(),
  value_schema: z.record(z.string(), z.unknown()),
  sensitive: z.boolean(),
  applies_to_new_tasks: z.boolean(),
  rollback_allowed: z.boolean(),
})

const settingSchema = z.object({
  key: settingKeySchema,
  value: z.record(z.string(), z.unknown()),
  schema_version: z.number().int().positive(),
  version: z.number().int().positive(),
  description: z.string(),
  updated_by: z.string().nullable(),
  updated_at: z.string(),
  definition: definitionSchema,
  allowed_actions: z.array(settingActionSchema).optional().default([]),
})

const historySchema = z.object({
  version: z.number().int().positive(),
  value: z.record(z.string(), z.unknown()),
  reason: z.string(),
  actor_user_id: z.string(),
  request_id: z.string(),
  created_at: z.string(),
  allowed_actions: z.array(settingActionSchema).optional().default([]),
})

const secretSchema = z.object({
  key: secretKeySchema,
  configured: z.boolean(),
  masked: z.string().nullable(),
  version: z.number().int().nonnegative(),
  fingerprint: z.string().nullable(),
  updated_at: z.string().nullable(),
  definition: definitionSchema,
  allowed_actions: z.array(secretActionSchema).optional().default([]),
})

function mapDefinition(item: z.infer<typeof definitionSchema>) {
  return {
    valueType: item.value_type,
    defaultValue: item.default_value,
    valueSchema: item.value_schema,
    sensitive: item.sensitive,
    appliesToNewTasks: item.applies_to_new_tasks,
    rollbackAllowed: item.rollback_allowed,
  }
}

function parse<T>(schema: z.ZodType<T>, value: unknown, code: string): T {
  const result = schema.safeParse(value)
  if (!result.success) {
    throw new ApiError("设置接口返回的数据无法识别。", {
      code,
      cause: result.error,
    })
  }
  return result.data
}

function mapSetting(item: z.infer<typeof settingSchema>): SettingItem {
  return {
    key: item.key,
    value: item.value,
    schemaVersion: item.schema_version,
    version: item.version,
    description: item.description,
    updatedBy: item.updated_by,
    updatedAt: item.updated_at,
    definition: mapDefinition(item.definition),
    allowedActions: item.allowed_actions,
  }
}

function mapSecret(item: z.infer<typeof secretSchema>): SecretStatus {
  return {
    key: item.key,
    configured: item.configured,
    masked: item.masked,
    version: item.version,
    fingerprint: item.fingerprint,
    updatedAt: item.updated_at,
    definition: mapDefinition(item.definition),
    allowedActions: item.allowed_actions,
  }
}

export function createSettingsGateway(baseUrl = ""): SettingsGateway {
  const api = createApiClient<paths>({ baseUrl })

  return {
    async loadOverview() {
      const [settingsValue, secretsValue] = await Promise.all([
        api.request<unknown>(api.client.GET("/api/v1/settings")),
        api.request<unknown>(api.client.GET("/api/v1/secrets/status")),
      ])
      const settings = parse(
        z.object({ items: z.array(z.unknown()) }),
        settingsValue,
        "SETTINGS_LIST_INVALID",
      )
      const secrets = parse(
        z.object({ items: z.array(z.unknown()) }),
        secretsValue,
        "SECRETS_STATUS_INVALID",
      )
      return {
        settings: settings.items.flatMap((item) => {
          const key = z.object({ key: z.string() }).safeParse(item)
          if (!key.success || !settingKeySet.has(key.data.key)) return []
          return [mapSetting(parse(settingSchema, item, "SETTING_ITEM_INVALID"))]
        }),
        secrets: secrets.items.flatMap((item) => {
          const key = z.object({ key: z.string() }).safeParse(item)
          if (!key.success || !secretKeySet.has(key.data.key)) return []
          return [mapSecret(parse(secretSchema, item, "SECRET_STATUS_INVALID"))]
        }),
      }
    },

    async loadHistory(key) {
      const value = await api.request<unknown>(
        api.client.GET("/api/v1/settings/{key}/history", {
          params: { path: { key } },
        }),
      )
      return parse(
        z.object({ items: z.array(historySchema) }),
        value,
        "SETTING_HISTORY_INVALID",
      ).items.map((item): SettingHistoryItem => ({
        version: item.version,
        value: item.value,
        reason: item.reason,
        actorUserId: item.actor_user_id,
        requestId: item.request_id,
        createdAt: item.created_at,
        allowedActions: item.allowed_actions,
      }))
    },

    async updateSetting(input) {
      const value = await api.request<unknown>(
        api.client.PATCH("/api/v1/settings/{key}", {
          params: {
            path: { key: input.key },
            header: { "Idempotency-Key": createClientIdempotencyKey() },
          },
          body: {
            value: input.value,
            expected_version: input.expectedVersion,
            reason: input.reason,
            confirm: true,
          },
        }),
      )
      return mapSetting(parse(settingSchema, value, "SETTING_UPDATE_INVALID"))
    },

    async rollbackSetting(input) {
      const value = await api.request<unknown>(
        api.client.POST("/api/v1/settings/{key}/rollback", {
          params: {
            path: { key: input.key },
            header: { "Idempotency-Key": createClientIdempotencyKey() },
          },
          body: {
            source_version: input.sourceVersion,
            expected_version: input.expectedVersion,
            reason: input.reason,
            confirm: true,
          },
        }),
      )
      return mapSetting(parse(settingSchema, value, "SETTING_ROLLBACK_INVALID"))
    },

    async updateSecret(input) {
      const value = await api.request<unknown>(
        api.client.PATCH("/api/v1/secrets/{key}", {
          params: {
            path: { key: input.key },
            header: { "Idempotency-Key": createClientIdempotencyKey() },
          },
          body: {
            value: input.value,
            clear_secret: input.clearSecret,
            expected_version: input.expectedVersion,
            reason: input.reason,
            confirm: true,
          },
        }),
      )
      return mapSecret(parse(secretSchema, value, "SECRET_UPDATE_INVALID"))
    },
  }
}

export const settingsGateway = createSettingsGateway()
