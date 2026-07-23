import { z } from "zod"

import type { SettingKey, SettingValue } from "@/features/settings/types"

const channels = z.array(z.enum(["WECOM", "EMAIL"])).max(2)
const notificationPolicy = z.object({
  enabled: z.boolean(),
  channels,
}).strict()
const systemAlertPolicy = z.object({
  enabled: z.boolean(),
  warning: channels,
  error: channels,
  critical: channels,
  recovered: channels,
  daily_unresolved: channels,
}).strict()
const wecomChannel = z.object({
  enabled: z.boolean(),
  timeout_seconds: z.number().min(1).max(15),
}).strict()
const emailChannel = z.object({
  enabled: z.boolean(),
  smtp_host: z.string().max(253),
  smtp_port: z.number().int().min(1).max(65535),
  security: z.enum(["SSL", "STARTTLS"]),
  username: z.string().max(320),
  sender: z.string().max(320),
  recipients: z.array(z.string().min(1).max(320)).max(5),
  timeout_seconds: z.number().min(1).max(30),
}).strict()

export interface SettingDefinition {
  label: string
  description: string
  schema: z.ZodType<SettingValue>
  affectsNewTasks: boolean
  allowsRollback: boolean
}

export const settingDefinitions: Record<SettingKey, SettingDefinition> = {
  "notification.policy.global": {
    label: "全局通知策略",
    description: "全局通知开关和默认发送渠道",
    schema: notificationPolicy,
    affectsNewTasks: true,
    allowsRollback: true,
  },
  "notification.policy.signals": {
    label: "信号通知策略",
    description: "股票信号通知开关和发送渠道",
    schema: notificationPolicy,
    affectsNewTasks: true,
    allowsRollback: true,
  },
  "notification.policy.system_alerts": {
    label: "系统告警策略",
    description: "不同严重程度和恢复提醒的发送渠道",
    schema: systemAlertPolicy,
    affectsNewTasks: true,
    allowsRollback: true,
  },
  "notification.channel.wecom": {
    label: "企业微信渠道",
    description: "企业微信机器人的启用状态和请求超时",
    schema: wecomChannel,
    affectsNewTasks: true,
    allowsRollback: true,
  },
  "notification.channel.email": {
    label: "邮件渠道",
    description: "邮件服务器、发件人、固定收件人和请求超时",
    schema: emailChannel,
    affectsNewTasks: true,
    allowsRollback: true,
  },
}

export function validateSettingValue(key: SettingKey, value: SettingValue) {
  return settingDefinitions[key].schema.safeParse(value)
}
