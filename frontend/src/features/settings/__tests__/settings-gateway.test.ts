import { http, HttpResponse } from "msw"
import { setupServer } from "msw/node"
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest"

import { createSettingsGateway } from "@/features/settings"
import type { ApiEnvelope } from "@/shared/api/client"

const server = setupServer()

beforeAll(() => server.listen({ onUnhandledRequest: "error" }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

function envelope<T>(data: T): ApiEnvelope<T> {
  return {
    success: true,
    code: "OK",
    message: "操作成功",
    data,
    request_id: "req-settings",
    server_time: "2026-07-23T03:00:00Z",
  }
}

const definition = {
  value_type: "object",
  default_value: { enabled: true, channels: [] },
  value_schema: { type: "object" },
  sensitive: false,
  applies_to_new_tasks: true,
  rollback_allowed: true,
}

const setting = {
  key: "notification.policy.global",
  value: { enabled: true, channels: ["WECOM"] },
  schema_version: 1,
  version: 3,
  description: "全局通知开关和默认渠道",
  updated_by: "user-1",
  updated_at: "2026-07-23T02:00:00Z",
  definition,
  allowed_actions: ["UPDATE", "ROLLBACK"],
}

const secret = {
  key: "notification.wecom.webhook",
  configured: true,
  masked: "********",
  version: 2,
  fingerprint: "abc123",
  updated_at: "2026-07-23T02:00:00Z",
  definition: {
    ...definition,
    default_value: null,
    sensitive: true,
    rollback_allowed: false,
  },
  allowed_actions: ["UPDATE", "CLEAR"],
}

describe("系统设置请求边界", () => {
  it("只保留白名单项目，并解析定义和后端允许操作", async () => {
    server.use(
      http.get("http://localhost/api/v1/settings", () => (
        HttpResponse.json(envelope({
          items: [
            setting,
            { ...setting, key: "unsafe.arbitrary.key" },
          ],
        }))
      )),
      http.get("http://localhost/api/v1/secrets/status", () => (
        HttpResponse.json(envelope({
          items: [
            secret,
            { ...secret, key: "database.password" },
          ],
        }))
      )),
    )

    const result = await createSettingsGateway("http://localhost").loadOverview()

    expect(result.settings).toHaveLength(1)
    expect(result.settings[0]).toEqual(expect.objectContaining({
      key: "notification.policy.global",
      allowedActions: ["UPDATE", "ROLLBACK"],
      definition: expect.objectContaining({
        sensitive: false,
        appliesToNewTasks: true,
        rollbackAllowed: true,
      }),
    }))
    expect(result.secrets).toHaveLength(1)
    expect(result.secrets[0]).toEqual(expect.objectContaining({
      masked: "********",
      allowedActions: ["UPDATE", "CLEAR"],
    }))
  })

  it("读取历史版本自己的回滚权限", async () => {
    server.use(
      http.get(
        "http://localhost/api/v1/settings/notification.policy.global/history",
        () => HttpResponse.json(envelope({
          items: [{
            version: 2,
            value: { enabled: false, channels: [] },
            reason: "临时停用",
            actor_user_id: "user-1",
            request_id: "req-history",
            created_at: "2026-07-22T02:00:00Z",
            allowed_actions: ["ROLLBACK"],
          }],
        })),
      ),
    )

    const result = await createSettingsGateway("http://localhost")
      .loadHistory("notification.policy.global")

    expect(result[0]).toEqual(expect.objectContaining({
      version: 2,
      allowedActions: ["ROLLBACK"],
    }))
  })

  it("保存和清空均携带版本、确认、原因和幂等键", async () => {
    const requests: Array<{ body: unknown; key: string | null }> = []
    server.use(
      http.patch(
        "http://localhost/api/v1/settings/notification.policy.global",
        async ({ request }) => {
          requests.push({
            body: await request.json(),
            key: request.headers.get("Idempotency-Key"),
          })
          return HttpResponse.json(envelope({ ...setting, version: 4 }))
        },
      ),
      http.patch(
        "http://localhost/api/v1/secrets/notification.wecom.webhook",
        async ({ request }) => {
          requests.push({
            body: await request.json(),
            key: request.headers.get("Idempotency-Key"),
          })
          return HttpResponse.json(envelope({
            ...secret,
            configured: false,
            masked: null,
            version: 3,
            fingerprint: null,
            allowed_actions: ["UPDATE"],
          }))
        },
      ),
    )
    const gateway = createSettingsGateway("http://localhost")

    await gateway.updateSetting({
      key: "notification.policy.global",
      value: { enabled: false, channels: [] },
      expectedVersion: 3,
      reason: "暂停所有通知",
    })
    await gateway.updateSecret({
      key: "notification.wecom.webhook",
      value: null,
      clearSecret: true,
      expectedVersion: 2,
      reason: "停用旧机器人",
    })

    expect(requests[0].body).toEqual({
      value: { enabled: false, channels: [] },
      expected_version: 3,
      reason: "暂停所有通知",
      confirm: true,
    })
    expect(requests[1].body).toEqual({
      value: null,
      clear_secret: true,
      expected_version: 2,
      reason: "停用旧机器人",
      confirm: true,
    })
    expect(requests.every((item) => Boolean(item.key))).toBe(true)
  })
})
