import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  ArchiveRestore,
  History,
  KeyRound,
  RefreshCw,
  Save,
  Settings2,
  ShieldCheck,
} from "lucide-react"
import { useEffect, useState } from "react"

import { useAuth } from "@/features/auth"
import {
  settingDefinitions,
  validateSettingValue,
} from "@/features/settings/definitions"
import { settingsGateway } from "@/features/settings/gateway"
import type {
  DeliveryChannel,
  SecretKey,
  SecretStatus,
  SettingHistoryItem,
  SettingItem,
  SettingKey,
  SettingsGateway,
  SettingValue,
} from "@/features/settings/types"
import { ApiError } from "@/shared/api/client"
import { Alert, AlertDescription } from "@/shared/ui/alert"
import { Button } from "@/shared/ui/button"
import { Checkbox } from "@/shared/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/shared/ui/dialog"
import { Input } from "@/shared/ui/input"
import { PageState } from "@/shared/ui/page-state"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/select"
import {
  Tabs,
  TabsList,
  TabsTrigger,
} from "@/shared/ui/tabs"
import { Textarea } from "@/shared/ui/textarea"

type SettingOperation =
  | { kind: "save"; setting: SettingItem; value: SettingValue }
  | { kind: "rollback"; setting: SettingItem; history: SettingHistoryItem }

const secretLabels: Record<SecretKey, string> = {
  "notification.wecom.webhook": "企业微信 Webhook",
  "notification.email.password": "邮件服务密码",
}

function formatTime(value: string | null) {
  if (!value) return "暂无"
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date)
}

function booleanValue(value: SettingValue, key: string) {
  return value[key] === true
}

function numberValue(value: SettingValue, key: string, fallback: number) {
  return typeof value[key] === "number" ? value[key] : fallback
}

function stringValue(value: SettingValue, key: string) {
  return typeof value[key] === "string" ? value[key] : ""
}

function channelsValue(value: SettingValue, key: string): DeliveryChannel[] {
  const candidate = value[key]
  if (!Array.isArray(candidate)) return []
  return candidate.filter(
    (item): item is DeliveryChannel => item === "WECOM" || item === "EMAIL",
  )
}

function ChannelPicker({
  label,
  channels,
  disabled,
  onChange,
}: {
  label: string
  channels: DeliveryChannel[]
  disabled: boolean
  onChange(value: DeliveryChannel[]): void
}) {
  return (
    <fieldset className="grid gap-2">
      <legend className="text-sm font-medium">{label}</legend>
      <div className="flex gap-4">
        {([
          ["WECOM", "企业微信"],
          ["EMAIL", "邮件"],
        ] as const).map(([channel, name]) => (
          <label className="flex items-center gap-2 text-sm" key={channel}>
            <Checkbox
              disabled={disabled}
              checked={channels.includes(channel)}
              onCheckedChange={(checked) => {
                onChange(checked === true
                  ? [...channels, channel]
                  : channels.filter((item) => item !== channel))
              }}
            />
            {name}
          </label>
        ))}
      </div>
    </fieldset>
  )
}

function SettingFields({
  setting,
  value,
  disabled,
  onChange,
}: {
  setting: SettingItem
  value: SettingValue
  disabled: boolean
  onChange(value: SettingValue): void
}) {
  const update = (key: string, next: unknown) => onChange({ ...value, [key]: next })
  const enabled = booleanValue(value, "enabled")

  if (
    setting.key === "notification.policy.global"
    || setting.key === "notification.policy.signals"
  ) {
    return (
      <div className="grid gap-5">
        <label className="flex items-center gap-2 text-sm font-medium">
          <Checkbox
            disabled={disabled}
            checked={enabled}
            onCheckedChange={(checked) => update("enabled", checked === true)}
          />
          启用此通知策略
        </label>
        <ChannelPicker
          label="发送渠道"
          disabled={disabled}
          channels={channelsValue(value, "channels")}
          onChange={(channels) => update("channels", channels)}
        />
      </div>
    )
  }

  if (setting.key === "notification.policy.system_alerts") {
    return (
      <div className="grid gap-5">
        <label className="flex items-center gap-2 text-sm font-medium">
          <Checkbox
            disabled={disabled}
            checked={enabled}
            onCheckedChange={(checked) => update("enabled", checked === true)}
          />
          启用系统告警通知
        </label>
        {([
          ["warning", "警告"],
          ["error", "错误"],
          ["critical", "严重"],
          ["recovered", "恢复通知"],
          ["daily_unresolved", "每日未解决提醒"],
        ] as const).map(([key, label]) => (
          <ChannelPicker
            key={key}
            label={label}
            disabled={disabled}
            channels={channelsValue(value, key)}
            onChange={(channels) => update(key, channels)}
          />
        ))}
      </div>
    )
  }

  if (setting.key === "notification.channel.wecom") {
    return (
      <div className="grid gap-5">
        <label className="flex items-center gap-2 text-sm font-medium">
          <Checkbox
            disabled={disabled}
            checked={enabled}
            onCheckedChange={(checked) => update("enabled", checked === true)}
          />
          启用企业微信渠道
        </label>
        <label className="grid gap-2 text-sm font-medium">
          请求超时（1 到 15 秒）
          <Input
            type="number"
            min={1}
            max={15}
            step={0.5}
            disabled={disabled}
            value={numberValue(value, "timeout_seconds", 5)}
            onChange={(event) => update("timeout_seconds", Number(event.target.value))}
          />
        </label>
      </div>
    )
  }

  return (
    <div className="grid gap-5 sm:grid-cols-2">
      <label className="flex items-center gap-2 text-sm font-medium sm:col-span-2">
        <Checkbox
          disabled={disabled}
          checked={enabled}
          onCheckedChange={(checked) => update("enabled", checked === true)}
        />
        启用邮件渠道
      </label>
      <label className="grid gap-2 text-sm font-medium">
        SMTP 主机
        <Input
          disabled={disabled}
          maxLength={253}
          value={stringValue(value, "smtp_host")}
          onChange={(event) => update("smtp_host", event.target.value)}
        />
      </label>
      <label className="grid gap-2 text-sm font-medium">
        SMTP 端口
        <Input
          type="number"
          min={1}
          max={65535}
          disabled={disabled}
          value={numberValue(value, "smtp_port", 465)}
          onChange={(event) => update("smtp_port", Number(event.target.value))}
        />
      </label>
      <div className="grid gap-2 text-sm font-medium">
        <span>连接安全</span>
        <Select
          disabled={disabled}
          value={stringValue(value, "security") || "SSL"}
          onValueChange={(nextValue) => update("security", nextValue)}
        >
          <SelectTrigger className="w-full" aria-label="连接安全"><SelectValue /></SelectTrigger>
          <SelectContent><SelectGroup>
            <SelectItem value="SSL">SSL</SelectItem>
            <SelectItem value="STARTTLS">STARTTLS</SelectItem>
          </SelectGroup></SelectContent>
        </Select>
      </div>
      <label className="grid gap-2 text-sm font-medium">
        请求超时（1 到 30 秒）
        <Input
          type="number"
          min={1}
          max={30}
          step={0.5}
          disabled={disabled}
          value={numberValue(value, "timeout_seconds", 10)}
          onChange={(event) => update("timeout_seconds", Number(event.target.value))}
        />
      </label>
      <label className="grid gap-2 text-sm font-medium">
        用户名
        <Input
          disabled={disabled}
          maxLength={320}
          value={stringValue(value, "username")}
          onChange={(event) => update("username", event.target.value)}
        />
      </label>
      <label className="grid gap-2 text-sm font-medium">
        发件人
        <Input
          disabled={disabled}
          maxLength={320}
          value={stringValue(value, "sender")}
          onChange={(event) => update("sender", event.target.value)}
        />
      </label>
      <label className="grid gap-2 text-sm font-medium sm:col-span-2">
        固定收件人（每行一个，最多 5 个）
        <Textarea
          className="min-h-24"
          disabled={disabled}
          value={channelsAsText(value.recipients)}
          onChange={(event) => update(
            "recipients",
            event.target.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean),
          )}
        />
      </label>
    </div>
  )
}

function channelsAsText(value: unknown) {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string").join("\n")
    : ""
}

function SettingsPanel({
  gateway,
  settings,
}: {
  gateway: SettingsGateway
  settings: SettingItem[]
}) {
  const queryClient = useQueryClient()
  const [selectedKey, setSelectedKey] = useState<SettingKey | null>(
    settings[0]?.key ?? null,
  )
  const selected = settings.find((item) => item.key === selectedKey) ?? null
  const [draft, setDraft] = useState<SettingValue>(selected?.value ?? {})
  const [operation, setOperation] = useState<SettingOperation | null>(null)
  const [reason, setReason] = useState("")
  const [validationError, setValidationError] = useState("")
  const [successMessage, setSuccessMessage] = useState("")

  const historyQuery = useQuery({
    queryKey: ["settings", "history", selectedKey],
    queryFn: () => gateway.loadHistory(selectedKey!),
    enabled: selectedKey !== null,
  })
  const mutation = useMutation({
    mutationFn: async () => {
      if (!operation) throw new Error("没有待执行的配置操作。")
      if (operation.kind === "save") {
        return gateway.updateSetting({
          key: operation.setting.key,
          value: operation.value,
          expectedVersion: operation.setting.version,
          reason: reason.trim(),
        })
      }
      return gateway.rollbackSetting({
        key: operation.setting.key,
        sourceVersion: operation.history.version,
        expectedVersion: operation.setting.version,
        reason: reason.trim(),
      })
    },
    onSuccess: async (updated) => {
      setDraft(updated.value)
      setSuccessMessage(operation?.kind === "rollback" ? "历史版本已回滚为新版本" : "配置已保存")
      setOperation(null)
      setReason("")
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["settings", "overview"] }),
        queryClient.invalidateQueries({ queryKey: ["settings", "history", selectedKey] }),
      ])
    },
  })

  if (settings.length === 0) {
    return (
      <PageState
        state="empty"
        title="没有可编辑的配置"
        description="后端当前没有返回白名单内的普通配置。"
      />
    )
  }

  const prepareSave = () => {
    if (!selected?.allowedActions.includes("UPDATE")) return
    const validation = validateSettingValue(selected.key, draft)
    if (!validation.success) {
      setValidationError("配置内容不符合允许的类型或范围，请检查后再保存。")
      return
    }
    setValidationError("")
    setSuccessMessage("")
    mutation.reset()
    setReason("")
    setOperation({ kind: "save", setting: selected, value: validation.data })
  }

  return (
    <div className="grid gap-6 lg:grid-cols-[17rem_minmax(0,1fr)]">
      <nav className="border" aria-label="配置项目">
        {settings.map((item) => (
          <Button
            type="button"
            variant={item.key === selectedKey ? "secondary" : "ghost"}
            className="h-auto w-full justify-start rounded-none border-b p-4 text-left last:border-b-0"
            key={item.key}
            onClick={() => {
              setSelectedKey(item.key)
              setDraft(item.value)
              setValidationError("")
              setSuccessMessage("")
            }}
          >
            <strong className="block text-sm">{settingDefinitions[item.key].label}</strong>
            <span className="mt-1 block text-xs text-muted-foreground">
              版本 {item.version}
            </span>
          </Button>
        ))}
      </nav>

      {selected ? (
        <div className="min-w-0">
          <header className="mb-5 border-b pb-4">
            <h2 className="text-lg font-semibold">{settingDefinitions[selected.key].label}</h2>
            <p className="mt-1 text-sm text-muted-foreground">{selected.description}</p>
            <div className="mt-3 flex flex-wrap gap-3 text-xs text-muted-foreground">
              <span>当前版本 {selected.version}</span>
              <span>更新于 {formatTime(selected.updatedAt)}</span>
              {selected.definition.appliesToNewTasks ? <span>仅影响新任务</span> : null}
            </div>
          </header>

          <SettingFields
            setting={selected}
            value={draft}
            disabled={!selected.allowedActions.includes("UPDATE") || mutation.isPending}
            onChange={setDraft}
          />
          {validationError ? (
            <Alert className="mt-4" variant="destructive"><AlertDescription>{validationError}</AlertDescription></Alert>
          ) : null}
          {successMessage ? (
            <Alert className="mt-4"><AlertDescription role="status">{successMessage}</AlertDescription></Alert>
          ) : null}
          <div className="mt-5 flex justify-end">
            <Button
              type="button"
              disabled={!selected.allowedActions.includes("UPDATE") || mutation.isPending}
              onClick={prepareSave}
            >
              <Save data-icon="inline-start" aria-hidden="true" />保存配置
            </Button>
          </div>

          <section className="mt-8 border-t pt-5" aria-label="配置历史">
            <h3 className="mb-4 flex items-center gap-2 font-semibold">
              <History className="size-4" aria-hidden="true" />不可变历史
            </h3>
            {historyQuery.isPending ? (
              <p className="text-sm text-muted-foreground">正在读取历史...</p>
            ) : historyQuery.isError ? (
              <div className="flex items-center justify-between gap-3 text-sm" role="alert">
                <span>历史记录读取失败。</span>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => void historyQuery.refetch()}
                >
                  <RefreshCw data-icon="inline-start" aria-hidden="true" />重试
                </Button>
              </div>
            ) : historyQuery.data.length === 0 ? (
              <p className="text-sm text-muted-foreground">暂无历史版本。</p>
            ) : (
              <ol className="divide-y border">
                {historyQuery.data.map((item) => (
                  <li className="flex flex-wrap items-center justify-between gap-3 p-4" key={item.version}>
                    <div>
                      <strong className="text-sm">版本 {item.version}</strong>
                      <span className="ml-3 text-xs text-muted-foreground">
                        {formatTime(item.createdAt)}
                      </span>
                      <p className="mt-1 text-sm">{item.reason}</p>
                    </div>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={
                        !selected.definition.rollbackAllowed
                        || !selected.allowedActions.includes("ROLLBACK")
                        || !item.allowedActions.includes("ROLLBACK")
                        || mutation.isPending
                      }
                      onClick={() => {
                        mutation.reset()
                        setReason("")
                        setOperation({ kind: "rollback", setting: selected, history: item })
                      }}
                    >
                      <ArchiveRestore data-icon="inline-start" aria-hidden="true" />回滚到此版本
                    </Button>
                  </li>
                ))}
              </ol>
            )}
          </section>
        </div>
      ) : null}

      <Dialog
        open={operation !== null}
        onOpenChange={(open) => {
          if (!open && !mutation.isPending) {
            setOperation(null)
            setReason("")
          }
        }}
      >
        <DialogContent showCloseButton={false}>
          <DialogTitle>{operation?.kind === "rollback" ? "确认回滚配置" : "确认保存配置"}</DialogTitle>
          <DialogDescription>
            {operation?.kind === "rollback"
              ? "回滚会复制历史值并创建一个新版本，不会修改历史记录。"
              : "配置保存后只影响新创建的任务，正在运行的任务继续使用原快照。"}
          </DialogDescription>
          <label className="grid gap-2 text-sm font-medium">
            变更原因
            <Input
              aria-label="变更原因"
              maxLength={500}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
          {mutation.isError ? (
            <p className="text-sm text-destructive" role="alert">
              {mutation.error instanceof Error ? mutation.error.message : "配置操作失败。"}
            </p>
          ) : null}
          <div className="flex justify-end gap-2">
            <Button
              variant="outline"
              disabled={mutation.isPending}
              onClick={() => setOperation(null)}
            >
              返回
            </Button>
            <Button
              disabled={!reason.trim() || mutation.isPending}
              onClick={() => mutation.mutate()}
            >
              {mutation.isPending ? "正在提交" : "确认执行"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}

function SecretPanel({
  gateway,
  secrets,
}: {
  gateway: SettingsGateway
  secrets: SecretStatus[]
}) {
  const queryClient = useQueryClient()
  const [selectedKey, setSelectedKey] = useState<SecretKey | null>(secrets[0]?.key ?? null)
  const selected = secrets.find((item) => item.key === selectedKey) ?? null
  const [value, setValue] = useState("")
  const [reason, setReason] = useState("")
  const [clearSecret, setClearSecret] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [successMessage, setSuccessMessage] = useState("")

  const mutation = useMutation({
    mutationFn: () => gateway.updateSecret({
      key: selected!.key,
      value: clearSecret ? null : value || null,
      clearSecret,
      expectedVersion: selected!.version,
      reason: reason.trim(),
    }),
    onSuccess: async () => {
      setConfirming(false)
      setValue("")
      setReason("")
      setClearSecret(false)
      setSuccessMessage("敏感配置状态已更新")
      await queryClient.invalidateQueries({ queryKey: ["settings", "overview"] })
    },
  })

  if (secrets.length === 0) {
    return (
      <PageState
        state="empty"
        title="没有敏感配置"
        description="后端当前没有返回白名单内的敏感项目。"
      />
    )
  }

  const canSubmit = selected
    ? clearSecret
      ? selected.allowedActions.includes("CLEAR")
      : selected.allowedActions.includes("UPDATE")
    : false

  return (
    <div className="grid gap-6 lg:grid-cols-[17rem_minmax(0,1fr)]">
      <nav className="border" aria-label="敏感配置项目">
        {secrets.map((item) => (
          <Button
            type="button"
            variant={item.key === selectedKey ? "secondary" : "ghost"}
            className="h-auto w-full justify-start rounded-none border-b p-4 text-left last:border-b-0"
            key={item.key}
            onClick={() => {
              setSelectedKey(item.key)
              setValue("")
              setReason("")
              setClearSecret(false)
              setSuccessMessage("")
            }}
          >
            <strong className="block text-sm">{secretLabels[item.key]}</strong>
            <span className="mt-1 block text-xs text-muted-foreground">
              {item.configured ? "已配置" : "未配置"}
            </span>
          </Button>
        ))}
      </nav>

      {selected ? (
        <section aria-label={secretLabels[selected.key]}>
          <header className="mb-5 border-b pb-4">
            <h2 className="text-lg font-semibold">{secretLabels[selected.key]}</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              当前状态：{selected.configured ? `已配置 ${selected.masked ?? "********"}` : "未配置"}
            </p>
            {selected.fingerprint ? (
              <code className="mt-2 block break-all text-xs text-muted-foreground">
                指纹：{selected.fingerprint}
              </code>
            ) : null}
          </header>
          <div className="grid gap-5">
            <label className="grid gap-2 text-sm font-medium">
              新值
              <Input
                type="password"
                autoComplete="new-password"
                maxLength={4096}
                disabled={clearSecret || !selected.allowedActions.includes("UPDATE")}
                value={value}
                onChange={(event) => setValue(event.target.value)}
                placeholder={selected.configured ? "留空表示保留原值" : "输入新值"}
              />
            </label>
            {selected.configured ? (
              <label className="flex items-center gap-2 text-sm font-medium">
                <Checkbox
                  checked={clearSecret}
                  disabled={!selected.allowedActions.includes("CLEAR")}
                  onCheckedChange={(checked) => {
                    setClearSecret(checked === true)
                    if (checked === true) setValue("")
                  }}
                />
                明确清空当前敏感值
              </label>
            ) : null}
            <p className="text-xs text-muted-foreground">
              页面永远不会显示原始敏感值。留空保存会保留当前值，只有勾选清空才会删除。
            </p>
            {successMessage ? (
              <Alert><AlertDescription role="status">{successMessage}</AlertDescription></Alert>
            ) : null}
            <div className="flex justify-end">
              <Button
                disabled={!canSubmit || mutation.isPending}
                onClick={() => {
                  mutation.reset()
                  setReason("")
                  setConfirming(true)
                }}
              >
                <ShieldCheck data-icon="inline-start" aria-hidden="true" />
                {clearSecret ? "清空敏感值" : "保存敏感配置"}
              </Button>
            </div>
          </div>
        </section>
      ) : null}

      <Dialog
        open={confirming}
        onOpenChange={(open) => {
          if (!open && !mutation.isPending) setConfirming(false)
        }}
      >
        <DialogContent showCloseButton={false}>
          <DialogTitle>{clearSecret ? "确认清空敏感值" : "确认更新敏感配置"}</DialogTitle>
          <DialogDescription>
            {clearSecret
              ? "清空后相关通知渠道可能无法工作，此操作会被审计。"
              : value ? "新值会在服务器加密保存，提交后不会再次显示。" : "输入留空，当前敏感值将保持不变。"}
          </DialogDescription>
          <label className="grid gap-2 text-sm font-medium">
            变更原因
            <Input
              aria-label="敏感配置变更原因"
              maxLength={500}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
          {mutation.isError ? (
            <p className="text-sm text-destructive" role="alert">
              {mutation.error instanceof Error ? mutation.error.message : "敏感配置更新失败。"}
            </p>
          ) : null}
          <div className="flex justify-end gap-2">
            <Button
              variant="outline"
              disabled={mutation.isPending}
              onClick={() => setConfirming(false)}
            >
              返回
            </Button>
            <Button
              disabled={!reason.trim() || mutation.isPending}
              onClick={() => mutation.mutate()}
            >
              {mutation.isPending ? "正在提交" : "确认执行"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}

export function SettingsPage({
  gateway = settingsGateway,
}: {
  gateway?: SettingsGateway
}) {
  const { invalidate } = useAuth()
  const [tab, setTab] = useState<"settings" | "secrets">("settings")
  const overviewQuery = useQuery({
    queryKey: ["settings", "overview"],
    queryFn: () => gateway.loadOverview(),
  })

  useEffect(() => {
    if (overviewQuery.error instanceof ApiError && overviewQuery.error.status === 401) {
      invalidate()
    }
  }, [invalidate, overviewQuery.error])

  return (
    <main className="mx-auto w-full max-w-7xl px-4 py-5 sm:px-6">
      <header className="mb-5 flex flex-wrap items-center justify-between gap-4 border-b pb-4">
        <div className="flex items-center gap-3">
          <span className="grid size-10 place-items-center bg-muted">
            <Settings2 aria-hidden="true" />
          </span>
          <div>
            <p className="text-sm text-muted-foreground">系统管理</p>
            <h1 className="text-2xl font-semibold">系统设置</h1>
          </div>
        </div>
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label="刷新系统设置"
          disabled={overviewQuery.isFetching}
          onClick={() => void overviewQuery.refetch()}
        >
          <RefreshCw data-icon="icon" className={overviewQuery.isFetching ? "animate-spin" : undefined} />
        </Button>
      </header>

      <Tabs className="mb-6" value={tab} onValueChange={(value) => setTab(value as "settings" | "secrets")}>
        <TabsList aria-label="设置类别">
          <TabsTrigger value="settings"><Settings2 data-icon="inline-start" />普通配置</TabsTrigger>
          <TabsTrigger value="secrets"><KeyRound data-icon="inline-start" />敏感配置</TabsTrigger>
        </TabsList>
      </Tabs>

      {overviewQuery.isPending ? (
        <PageState state="loading" title="正在读取系统设置" description="正在读取白名单配置和敏感项状态。" />
      ) : overviewQuery.isError ? (
        <PageState
          state="error"
          title="系统设置读取失败"
          description="请检查连接后重试，当前不会提交任何变更。"
          error={overviewQuery.error instanceof ApiError
            ? { code: overviewQuery.error.code, requestId: overviewQuery.error.requestId }
            : { code: "SETTINGS_UNAVAILABLE" }}
          action={{ label: "重新读取", onClick: () => void overviewQuery.refetch() }}
        />
      ) : overviewQuery.data.settings.length === 0
        && overviewQuery.data.secrets.length === 0 ? (
          <PageState state="empty" title="没有可管理的设置" description="后端没有返回白名单内的设置项目。" />
        ) : tab === "settings" ? (
          <SettingsPanel gateway={gateway} settings={overviewQuery.data.settings} />
        ) : (
          <SecretPanel gateway={gateway} secrets={overviewQuery.data.secrets} />
        )}
    </main>
  )
}
