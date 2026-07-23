export const settingKeys = [
  "notification.policy.global",
  "notification.policy.signals",
  "notification.policy.system_alerts",
  "notification.channel.wecom",
  "notification.channel.email",
] as const

export const secretKeys = [
  "notification.wecom.webhook",
  "notification.email.password",
] as const

export type SettingKey = typeof settingKeys[number]
export type SecretKey = typeof secretKeys[number]
export type SettingAction = "UPDATE" | "ROLLBACK"
export type SecretAction = "UPDATE" | "CLEAR"
export type DeliveryChannel = "WECOM" | "EMAIL"

export type SettingValue = Record<string, unknown>

export interface SettingDefinitionMetadata {
  valueType: string
  defaultValue: unknown
  valueSchema: Record<string, unknown>
  sensitive: boolean
  appliesToNewTasks: boolean
  rollbackAllowed: boolean
}

export interface SettingItem {
  key: SettingKey
  value: SettingValue
  schemaVersion: number
  version: number
  description: string
  updatedBy: string | null
  updatedAt: string
  definition: SettingDefinitionMetadata
  allowedActions: SettingAction[]
}

export interface SettingHistoryItem {
  version: number
  value: SettingValue
  reason: string
  actorUserId: string
  requestId: string
  createdAt: string
  allowedActions: SettingAction[]
}

export interface SecretStatus {
  key: SecretKey
  configured: boolean
  masked: string | null
  version: number
  fingerprint: string | null
  updatedAt: string | null
  definition: SettingDefinitionMetadata
  allowedActions: SecretAction[]
}

export interface SettingsOverview {
  settings: SettingItem[]
  secrets: SecretStatus[]
}

export interface SettingsGateway {
  loadOverview(): Promise<SettingsOverview>
  loadHistory(key: SettingKey): Promise<SettingHistoryItem[]>
  updateSetting(input: {
    key: SettingKey
    value: SettingValue
    expectedVersion: number
    reason: string
  }): Promise<SettingItem>
  rollbackSetting(input: {
    key: SettingKey
    sourceVersion: number
    expectedVersion: number
    reason: string
  }): Promise<SettingItem>
  updateSecret(input: {
    key: SecretKey
    value: string | null
    clearSecret: boolean
    expectedVersion: number
    reason: string
  }): Promise<SecretStatus>
}
