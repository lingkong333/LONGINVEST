import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  AlertTriangle,
  BellRing,
  Cable,
  CheckCircle2,
  CircleX,
  Clock3,
  FileText,
  FlaskConical,
  ListChecks,
  Mail,
  MessageSquareText,
  Pencil,
  RefreshCw,
  RotateCcw,
  Save,
  Send,
} from "lucide-react"
import { useEffect, useMemo, useState } from "react"

import { useAuth } from "@/features/auth"
import { notificationGateway } from "@/features/notifications/gateway"
import type {
  DeliveryChannel,
  NotificationAction,
  NotificationDelivery,
  NotificationGateway,
  NotificationChannel,
  NotificationPolicy,
  NotificationTemplate,
  PolicyScope,
} from "@/features/notifications/types"
import { ApiError } from "@/shared/api/client"
import { Alert, AlertDescription } from "@/shared/ui/alert"
import { Badge } from "@/shared/ui/badge"
import { Button } from "@/shared/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/shared/ui/card"
import { Checkbox } from "@/shared/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogTitle,
} from "@/shared/ui/dialog"
import {
  Field,
  FieldLabel,
  FieldLegend,
  FieldSet,
} from "@/shared/ui/field"
import { Input } from "@/shared/ui/input"
import { NativeSelect, NativeSelectOption } from "@/shared/ui/native-select"
import { PageState } from "@/shared/ui/page-state"
import {
  Table,
  TableBody,
  TableCell,
  TableFooter,
  TableHead,
  TableHeader,
  TableRow,
} from "@/shared/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/shared/ui/tabs"
import { Textarea } from "@/shared/ui/textarea"

type View = "events" | "deliveries" | "channels" | "policies" | "templates"

const views = [
  { id: "events", label: "通知事件", icon: BellRing },
  { id: "deliveries", label: "渠道投递", icon: Send },
  { id: "channels", label: "通知渠道", icon: Cable },
  { id: "policies", label: "通知策略", icon: ListChecks },
  { id: "templates", label: "模板版本", icon: FileText },
] satisfies { id: View; label: string; icon: typeof BellRing }[]

const channelLabels: Record<DeliveryChannel, string> = {
  WECOM: "企业微信",
  EMAIL: "邮件",
}

const statusLabels: Record<string, string> = {
  ELIGIBLE: "符合资格",
  SUPPRESSED: "已抑制",
  DISPATCHED: "已分发",
  PARTIAL: "部分成功",
  DELIVERED: "已送达",
  FAILED: "失败",
  CANCELED: "已取消",
  PENDING: "等待发送",
  SENDING: "发送中",
  SENT: "发送成功",
  RETRY_WAIT: "等待重试",
  OUTCOME_UNKNOWN: "结果未知",
  SKIPPED_DISABLED: "渠道已停用",
  SKIPPED_INELIGIBLE: "资格已失效",
}

const policyLabels: Record<PolicyScope, string> = {
  global: "全局默认",
  signals: "股票信号",
  "system-alerts": "系统告警",
}

function formatTime(value: string | null) {
  if (!value) return "暂无"
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value))
}

function hasAction(actions: NotificationAction[], action: NotificationAction) {
  return actions.includes(action)
}

function errorDetails(error: unknown, fallback: string) {
  return error instanceof ApiError
    ? { code: error.code, requestId: error.requestId }
    : { code: fallback }
}

export function NotificationsPage({
  gateway = notificationGateway,
}: {
  gateway?: NotificationGateway
}) {
  const { invalidate } = useAuth()
  const [view, setView] = useState<View>("events")

  return (
    <Tabs value={view} onValueChange={(value) => setView(value as View)} className="contents">
    <main className="mx-auto flex w-full max-w-[1500px] flex-col gap-6 px-4 py-6 lg:px-8">
      <header className="grid gap-5 border-b pb-5 xl:grid-cols-[1fr_auto] xl:items-end">
        <h1 className="text-3xl font-semibold">通知中心</h1>
        <TabsList className="max-w-full overflow-x-auto" aria-label="通知中心视图">
          {views.map(({ id, label, icon: Icon }) => (
            <TabsTrigger
              key={id}
              value={id}
            >
              <Icon aria-hidden="true" />
              {label}
            </TabsTrigger>
          ))}
        </TabsList>
      </header>

      <TabsContent value="events"><EventsView gateway={gateway} onUnauthorized={invalidate} /></TabsContent>
      <TabsContent value="deliveries"><DeliveriesView gateway={gateway} onUnauthorized={invalidate} /></TabsContent>
      <TabsContent value="channels"><ChannelsView gateway={gateway} onUnauthorized={invalidate} /></TabsContent>
      <TabsContent value="policies"><PoliciesView gateway={gateway} onUnauthorized={invalidate} /></TabsContent>
      <TabsContent value="templates"><TemplatesView gateway={gateway} onUnauthorized={invalidate} /></TabsContent>
    </main>
    </Tabs>
  )
}

function EventsView({
  gateway,
  onUnauthorized,
}: {
  gateway: NotificationGateway
  onUnauthorized: () => void
}) {
  const query = useQuery({
    queryKey: ["notifications", "events"],
    queryFn: gateway.loadEvents,
  })
  useUnauthorized(query.error, onUnauthorized)

  if (query.isPending) return <PageState state="loading" title="正在读取通知事件" description="正在加载冻结后的通知资格和渠道快照。" />
  if (query.isError) return <PageState state="error" title="通知事件暂时无法读取" description="投递任务不受影响，请稍后重新加载。" action={{ label: "重新加载", onClick: () => void query.refetch() }} error={errorDetails(query.error, "NOTIFICATION_EVENTS_FAILED")} />
  if (query.data.items.length === 0) return <PageState state="empty" title="暂无通知事件" description="符合条件的信号或系统告警出现后会显示在这里。" />

  return (
    <Card aria-label="通知事件列表">
      <CardContent className="px-0">
        <Table className="min-w-[960px]">
          <TableHeader>
            <TableRow><TableHead>发生时间</TableHead><TableHead>事件</TableHead><TableHead>业务对象</TableHead><TableHead>资格</TableHead><TableHead>渠道</TableHead><TableHead>状态</TableHead></TableRow>
          </TableHeader>
          <TableBody>
            {query.data.items.map((event) => (
              <TableRow key={event.id}>
                <TableCell>{formatTime(event.createdAt)}</TableCell>
                <TableCell>
                  <strong className="block">{event.eventType}</strong>
                  <span className="mt-1 block font-mono text-xs text-muted-foreground">{event.id.slice(0, 12)}</span>
                </TableCell>
                <TableCell><span className="block">{event.businessObjectType}</span><span className="mt-1 block max-w-56 truncate font-mono text-xs text-muted-foreground">{event.businessObjectId}</span></TableCell>
                <TableCell>
                  <span>{statusLabels[event.eligibilityStatus] ?? event.eligibilityStatus}</span>
                  {event.suppressionReason ? <span className="mt-1 block text-xs text-destructive">{event.suppressionReason}</span> : null}
                </TableCell>
                <TableCell>{event.effectiveChannels.length > 0 ? event.effectiveChannels.map((channel) => channelLabels[channel]).join("、") : "仅网页"}</TableCell>
                <TableCell><StatusBadge status={event.status} /></TableCell>
              </TableRow>
            ))}
          </TableBody>
          <TableFooter><TableRow><TableCell colSpan={6}>共 {query.data.total} 个事件</TableCell></TableRow></TableFooter>
        </Table>
      </CardContent>
    </Card>
  )
}

type DeliveryOperation =
  | { kind: "retry"; delivery: NotificationDelivery }
  | { kind: "cancel"; delivery: NotificationDelivery }

function DeliveriesView({
  gateway,
  onUnauthorized,
}: {
  gateway: NotificationGateway
  onUnauthorized: () => void
}) {
  const queryClient = useQueryClient()
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [operation, setOperation] = useState<DeliveryOperation | null>(null)
  const [reason, setReason] = useState("")
  const [duplicateConfirmed, setDuplicateConfirmed] = useState(false)
  const query = useQuery({
    queryKey: ["notifications", "deliveries"],
    queryFn: gateway.loadDeliveries,
  })
  const attempts = useQuery({
    queryKey: ["notifications", "attempts", selectedId],
    queryFn: () => gateway.loadAttempts(selectedId!),
    enabled: selectedId !== null,
  })
  const mutation = useMutation({
    mutationFn: async () => {
      if (!operation) return
      if (operation.kind === "retry") {
        await gateway.retryDelivery({
          deliveryId: operation.delivery.id,
          reason: reason.trim(),
          confirmDuplicateRisk: duplicateConfirmed,
        })
      } else {
        await gateway.cancelDelivery(operation.delivery.id, reason.trim())
      }
    },
    onSuccess: async () => {
      closeOperation()
      await queryClient.invalidateQueries({ queryKey: ["notifications", "deliveries"] })
    },
  })
  useUnauthorized(query.error ?? attempts.error ?? mutation.error, onUnauthorized)
  const delivery = query.data?.items.find((item) => item.id === selectedId) ?? null
  const hasDuplicateRisk = operation?.kind === "retry"
    && operation.delivery.requiresDuplicateConfirmation

  function closeOperation() {
    if (mutation.isPending) return
    setOperation(null)
    setReason("")
    setDuplicateConfirmed(false)
    mutation.reset()
  }

  if (query.isPending) return <PageState state="loading" title="正在读取渠道投递" description="正在加载各渠道的独立发送状态。" />
  if (query.isError) return <PageState state="error" title="渠道投递暂时无法读取" description="请稍后重新加载，后台重试仍按原计划执行。" action={{ label: "重新加载", onClick: () => void query.refetch() }} error={errorDetails(query.error, "NOTIFICATION_DELIVERIES_FAILED")} />
  if (query.data.items.length === 0) return <PageState state="empty" title="暂无渠道投递" description="通知事件产生渠道任务后会显示在这里。" />

  return (
    <>
      <Card className="grid min-h-[34rem] overflow-hidden py-0 xl:grid-cols-[minmax(0,1fr)_25rem]" aria-label="渠道投递与尝试">
        <div className="overflow-x-auto">
          <Table className="min-w-[780px]">
            <TableHeader>
              <TableRow><TableHead>渠道</TableHead><TableHead>状态</TableHead><TableHead>代数</TableHead><TableHead>尝试</TableHead><TableHead>更新时间</TableHead><TableHead className="text-right">操作</TableHead></TableRow>
            </TableHeader>
            <TableBody>
              {query.data.items.map((item) => (
                <TableRow key={item.id} data-state={item.id === selectedId ? "selected" : undefined}>
                  <TableCell>
                    <Button type="button" variant="ghost" className="h-auto justify-start px-0 text-left" onClick={() => setSelectedId(item.id)}>
                      <strong className="block">{channelLabels[item.channel]}</strong>
                    </Button>
                  </TableCell>
                  <TableCell><StatusBadge status={item.status} />{item.errorCode ? <span className="mt-1 block text-xs text-destructive">{item.errorCode}</span> : null}</TableCell>
                  <TableCell>第 {item.generation} 代</TableCell>
                  <TableCell>{item.attemptCount} 次</TableCell>
                  <TableCell>{formatTime(item.updatedAt)}</TableCell>
                  <TableCell>
                    <div className="flex justify-end gap-2">
                      <Button size="icon-sm" variant="outline" aria-label={`查看 ${channelLabels[item.channel]} 尝试`} title="查看发送尝试" onClick={() => setSelectedId(item.id)}><Clock3 /></Button>
                      <Button size="icon-sm" variant="outline" aria-label={`重试 ${channelLabels[item.channel]} 投递`} title={hasAction(item.allowedActions, "RETRY") ? "重试或重发此渠道" : "当前状态不允许重试"} disabled={!hasAction(item.allowedActions, "RETRY")} onClick={() => setOperation({ kind: "retry", delivery: item })}><RefreshCw /></Button>
                      <Button size="icon-sm" variant="outline" aria-label={`取消 ${channelLabels[item.channel]} 投递`} title={hasAction(item.allowedActions, "CANCEL") ? "取消此渠道投递" : "当前状态不允许取消"} disabled={!hasAction(item.allowedActions, "CANCEL")} onClick={() => setOperation({ kind: "cancel", delivery: item })}><CircleX /></Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
        <aside className="border-t bg-muted/15 p-5 xl:border-l xl:border-t-0" aria-label="发送尝试详情">
          <h2 className="font-semibold">发送尝试</h2>
          <p className="mt-1 text-xs text-muted-foreground">{delivery ? `${channelLabels[delivery.channel]} · 第 ${delivery.generation} 代` : "选择一条投递查看每次外部调用。"}</p>
          {!delivery ? <div className="mt-8 text-center text-sm text-muted-foreground">尚未选择投递</div> : attempts.isPending ? <div className="mt-8 text-sm text-muted-foreground">正在读取发送尝试…</div> : attempts.isError ? (
            <Alert variant="destructive" className="mt-6">
              <AlertDescription>尝试记录读取失败，不影响投递列表。</AlertDescription>
              <Button className="mt-3" size="sm" variant="outline" onClick={() => void attempts.refetch()}><RefreshCw />重新加载</Button>
            </Alert>
          ) : attempts.data.items.length === 0 ? <div className="mt-8 text-center text-sm text-muted-foreground">该投递尚未开始发送</div> : (
            <ol className="mt-5 space-y-3">
              {attempts.data.items.map((attempt) => (
                <li key={attempt.id} className="rounded-lg border bg-background p-3 text-sm">
                  <div className="flex items-center justify-between gap-3"><strong>第 {attempt.attemptNo} 次 · {attempt.phase}</strong><span>{attempt.durationMs === null ? "未完成" : `${attempt.durationMs} ms`}</span></div>
                  <p className="mt-2">{attempt.outcome}{attempt.possiblyDelivered ? " · 可能已送达" : ""}</p>
                  {attempt.errorCode ? <p className="mt-1 text-xs text-destructive">{attempt.errorCode}</p> : null}
                  {attempt.responseSummary ? <p className="mt-2 break-words text-xs text-muted-foreground">{attempt.responseSummary}</p> : null}
                </li>
              ))}
            </ol>
          )}
        </aside>
      </Card>

      <Dialog open={operation !== null} onOpenChange={(open) => { if (!open) closeOperation() }}>
        <DialogContent>
          <DialogTitle>{operation?.kind === "retry" ? "确认重试此渠道" : "确认取消此渠道"}</DialogTitle>
          <DialogDescription>
            {operation?.kind === "retry"
              ? "系统只处理这一条渠道投递，不会重发其他已成功渠道。"
              : "取消只适用于尚未发送或正在等待重试的投递。"}
          </DialogDescription>
          {hasDuplicateRisk ? (
            <Alert>
              <AlertDescription>
                <Field orientation="horizontal">
                  <Checkbox id="duplicate-confirmed" checked={duplicateConfirmed} onCheckedChange={(checked) => setDuplicateConfirmed(checked === true)} />
                  <FieldLabel htmlFor="duplicate-confirmed">我确认该投递可能已经送达，再次发送可能产生重复通知。系统会创建新的投递代数，不修改原记录。</FieldLabel>
                </Field>
              </AlertDescription>
            </Alert>
          ) : null}
          <label className="grid gap-2 text-sm font-medium">
            操作原因
            <Input value={reason} maxLength={500} placeholder="请说明本次人工操作原因" onChange={(event) => setReason(event.target.value)} />
          </label>
          {mutation.isError ? <MutationError error={mutation.error} /> : null}
          <DialogFooter>
            <Button variant="outline" disabled={mutation.isPending} onClick={closeOperation}>取消</Button>
            <Button disabled={!reason.trim() || mutation.isPending || (hasDuplicateRisk && !duplicateConfirmed)} onClick={() => mutation.mutate()}>
              {mutation.isPending ? "正在提交" : "确认执行"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}

type ChannelOperation = {
  channel: DeliveryChannel
  action: "TEST" | "PROBE" | "RESET_CIRCUIT"
}

function ChannelsView({ gateway, onUnauthorized }: { gateway: NotificationGateway; onUnauthorized: () => void }) {
  const queryClient = useQueryClient()
  const [operation, setOperation] = useState<ChannelOperation | null>(null)
  const [editing, setEditing] = useState<NotificationChannel | null>(null)
  const [reason, setReason] = useState("")
  const [message, setMessage] = useState("LongInvest 通知渠道连通性测试")
  const query = useQuery({ queryKey: ["notifications", "channels"], queryFn: gateway.loadChannels })
  const actionMutation = useMutation({
    mutationFn: () => gateway.runChannelAction({ ...operation!, reason: reason.trim(), message: message.trim() }),
    onSuccess: async () => {
      closeAction()
      await queryClient.invalidateQueries({ queryKey: ["notifications", "channels"] })
    },
  })
  const updateMutation = useMutation({
    mutationFn: () => gateway.updateChannel(editing!, reason.trim()),
    onSuccess: async () => {
      closeEdit()
      await queryClient.invalidateQueries({ queryKey: ["notifications", "channels"] })
    },
  })
  useUnauthorized(
    query.error ?? actionMutation.error ?? updateMutation.error,
    onUnauthorized,
  )

  function closeAction() {
    if (actionMutation.isPending) return
    setOperation(null)
    setReason("")
    setMessage("LongInvest 通知渠道连通性测试")
    actionMutation.reset()
  }

  function closeEdit() {
    if (updateMutation.isPending) return
    setEditing(null)
    setReason("")
    updateMutation.reset()
  }

  if (query.isPending) return <PageState state="loading" title="正在读取通知渠道" description="正在核对运行参数和密钥配置状态。" />
  if (query.isError) return <PageState state="error" title="通知渠道暂时无法读取" description="不会展示任何密钥原文，请稍后重新加载。" action={{ label: "重新加载", onClick: () => void query.refetch() }} error={errorDetails(query.error, "NOTIFICATION_CHANNELS_FAILED")} />

  return (
    <>
      <section className="grid gap-4 lg:grid-cols-2" aria-label="通知渠道">
        {query.data.map((channel) => {
          const Icon = channel.channel === "WECOM" ? MessageSquareText : Mail
          return (
            <Card key={channel.channel}>
              <CardHeader className="flex-row items-start justify-between gap-4">
                <div className="flex items-center gap-3"><div className="rounded-md bg-muted p-2"><Icon className="size-5" /></div><div><h2 className="font-semibold">{channelLabels[channel.channel]}</h2><p className="mt-1 text-xs text-muted-foreground">配置版本 v{channel.version}</p></div></div>
                <StatusBadge status={channel.enabled ? "ENABLED" : "DISABLED"} />
              </CardHeader>
              <CardContent>
              <dl className="grid gap-3 text-sm sm:grid-cols-2">
                <div><dt className="text-muted-foreground">密钥状态</dt><dd className="mt-1">{channel.secretConfigured ? "已配置" : "未配置"}</dd></div>
                <div><dt className="text-muted-foreground">连接超时</dt><dd className="mt-1">{channel.timeoutSeconds || "—"} 秒</dd></div>
                <div><dt className="text-muted-foreground">熔断状态</dt><dd className="mt-1">{circuitLabel(channel.circuitState)}</dd></div>
                <div><dt className="text-muted-foreground">连续失败</dt><dd className="mt-1">{channel.circuitFailures} 次</dd></div>
                {channel.circuitRetryAt ? <div className="sm:col-span-2"><dt className="text-muted-foreground">可探测时间</dt><dd className="mt-1">{formatTime(channel.circuitRetryAt)}</dd></div> : null}
                {channel.channel === "EMAIL" ? <><div><dt className="text-muted-foreground">服务器</dt><dd className="mt-1">{channel.smtpHost ? `${channel.smtpHost}:${channel.smtpPort}` : "未配置"}</dd></div><div><dt className="text-muted-foreground">安全方式</dt><dd className="mt-1">{channel.security ?? "—"}</dd></div><div className="sm:col-span-2"><dt className="text-muted-foreground">固定收件人</dt><dd className="mt-1">{channel.recipients.length > 0 ? `${channel.recipients.length} 个已配置地址` : "未配置"}</dd></div></> : null}
              </dl>
              <Alert className="mt-4"><AlertDescription>页面只显示是否已配置。密钥、密码和完整目标地址永远不会回显。</AlertDescription></Alert>
              <div className="mt-4 flex flex-wrap gap-2">
                <Button size="sm" variant="outline" disabled={!hasAction(channel.allowedActions, "UPDATE")} onClick={() => { setReason(""); setEditing({ ...channel }) }}><Pencil />编辑配置</Button>
                <Button size="sm" variant="outline" disabled={!hasAction(channel.allowedActions, "TEST")} onClick={() => setOperation({ channel: channel.channel, action: "TEST" })}><FlaskConical />发送测试</Button>
                <Button size="sm" variant="outline" disabled={!hasAction(channel.allowedActions, "PROBE")} onClick={() => setOperation({ channel: channel.channel, action: "PROBE" })}><Cable />连接探测</Button>
                <Button size="sm" variant="outline" disabled={!hasAction(channel.allowedActions, "RESET_CIRCUIT")} onClick={() => setOperation({ channel: channel.channel, action: "RESET_CIRCUIT" })}><RotateCcw />重置熔断</Button>
              </div>
              {channel.allowedActions.length === 0 ? <p className="mt-3 text-xs text-muted-foreground">后端未提供允许操作，当前渠道保持只读。</p> : null}
              </CardContent>
            </Card>
          )
        })}
      </section>
      <Dialog open={operation !== null} onOpenChange={(open) => { if (!open) closeAction() }}>
        <DialogContent>
          <DialogTitle>{operation ? `${channelLabels[operation.channel]} · ${channelActionLabel(operation.action)}` : "渠道操作"}</DialogTitle>
          <DialogDescription>操作会被记录。两个发送渠道相互隔离，不会因本次操作共同重启。</DialogDescription>
          {operation?.action !== "RESET_CIRCUIT" ? <label className="grid gap-2 text-sm font-medium">测试消息<Input value={message} maxLength={1000} onChange={(event) => setMessage(event.target.value)} /></label> : null}
          <label className="grid gap-2 text-sm font-medium">操作原因<Input value={reason} maxLength={500} onChange={(event) => setReason(event.target.value)} /></label>
          {actionMutation.isError ? <MutationError error={actionMutation.error} /> : null}
          <DialogFooter><Button variant="outline" disabled={actionMutation.isPending} onClick={closeAction}>取消</Button><Button disabled={!reason.trim() || actionMutation.isPending || (operation?.action !== "RESET_CIRCUIT" && !message.trim())} onClick={() => actionMutation.mutate()}>{actionMutation.isPending ? "正在执行" : "确认执行"}</Button></DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog open={editing !== null} onOpenChange={(open) => { if (!open) closeEdit() }}>
        <DialogContent>
          <DialogTitle>{editing ? `${channelLabels[editing.channel]} · 编辑配置` : "编辑渠道配置"}</DialogTitle>
          <DialogDescription>修改运行参数不会展示或覆盖已经保存的密钥。保存时使用当前配置版本防止互相覆盖。</DialogDescription>
          {editing ? (
            <>
              <Field orientation="horizontal">
                <Checkbox id="channel-enabled" checked={editing.enabled} disabled={updateMutation.isPending} onCheckedChange={(checked) => setEditing({ ...editing, enabled: checked === true })} />
                <FieldLabel htmlFor="channel-enabled">启用此渠道</FieldLabel>
              </Field>
              <label className="grid gap-2 text-sm font-medium">
                连接超时（秒）
                <Input type="number" min="1" max={editing.channel === "WECOM" ? "15" : "30"} value={editing.timeoutSeconds} disabled={updateMutation.isPending} onChange={(event) => setEditing({ ...editing, timeoutSeconds: Number(event.target.value) })} />
              </label>
              {editing.channel === "EMAIL" ? (
                <div className="grid gap-4 sm:grid-cols-2">
                  <label className="grid gap-2 text-sm font-medium sm:col-span-2">
                    SMTP 服务器
                    <Input value={editing.smtpHost ?? ""} maxLength={253} disabled={updateMutation.isPending} onChange={(event) => setEditing({ ...editing, smtpHost: event.target.value })} />
                  </label>
                  <label className="grid gap-2 text-sm font-medium">
                    SMTP 端口
                    <Input type="number" min="1" max="65535" value={editing.smtpPort ?? 465} disabled={updateMutation.isPending} onChange={(event) => setEditing({ ...editing, smtpPort: Number(event.target.value) })} />
                  </label>
                  <label className="grid gap-2 text-sm font-medium">
                    安全方式
                    <NativeSelect value={editing.security ?? "SSL"} disabled={updateMutation.isPending} onChange={(event) => setEditing({ ...editing, security: event.target.value })}>
                      <NativeSelectOption value="SSL">SSL</NativeSelectOption>
                      <NativeSelectOption value="STARTTLS">STARTTLS</NativeSelectOption>
                    </NativeSelect>
                  </label>
                </div>
              ) : null}
              <label className="grid gap-2 text-sm font-medium">
                修改原因
                <Input value={reason} maxLength={500} disabled={updateMutation.isPending} onChange={(event) => setReason(event.target.value)} />
              </label>
            </>
          ) : null}
          {updateMutation.isError ? <MutationError error={updateMutation.error} /> : null}
          <DialogFooter><Button variant="outline" disabled={updateMutation.isPending} onClick={closeEdit}>取消</Button><Button disabled={!editing || !reason.trim() || !validChannelConfiguration(editing) || updateMutation.isPending} onClick={() => updateMutation.mutate()}><Save />{updateMutation.isPending ? "正在保存" : "保存配置"}</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}

function PoliciesView({ gateway, onUnauthorized }: { gateway: NotificationGateway; onUnauthorized: () => void }) {
  const scopes: PolicyScope[] = ["global", "signals", "system-alerts"]
  return (
    <section className="grid gap-4 xl:grid-cols-3" aria-label="通知策略">
      {scopes.map((scope) => <PolicyCard key={scope} scope={scope} gateway={gateway} onUnauthorized={onUnauthorized} />)}
    </section>
  )
}

function PolicyCard({ scope, gateway, onUnauthorized }: { scope: PolicyScope; gateway: NotificationGateway; onUnauthorized: () => void }) {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<NotificationPolicy | null>(null)
  const [reason, setReason] = useState("")
  const query = useQuery({ queryKey: ["notifications", "policy", scope], queryFn: () => gateway.loadPolicy(scope) })
  const policy = draft ?? query.data ?? null
  const mutation = useMutation({
    mutationFn: () => gateway.updatePolicy(policy!, reason.trim()),
    onSuccess: async () => {
      setDraft(null)
      setReason("")
      await queryClient.invalidateQueries({ queryKey: ["notifications", "policy", scope] })
    },
  })
  useUnauthorized(query.error ?? mutation.error, onUnauthorized)

  if (query.isError) return <Alert variant="destructive"><AlertDescription><strong>{policyLabels[scope]}</strong><span className="mt-2 block">该策略暂时无法读取，其他策略不受影响。</span><Button className="mt-4" size="sm" variant="outline" onClick={() => void query.refetch()}><RefreshCw />重新加载</Button></AlertDescription></Alert>
  if (query.isPending || !policy) return <Card className="min-h-64"><CardContent><p className="text-sm text-muted-foreground">正在读取{policyLabels[scope]}策略…</p></CardContent></Card>

  const canUpdate = hasAction(policy.allowedActions, "UPDATE")
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between gap-3"><div><CardTitle><h2>{policyLabels[scope]}</h2></CardTitle><CardDescription>版本 v{policy.version}</CardDescription></div><Field orientation="horizontal"><Checkbox id={`policy-${scope}-enabled`} checked={policy.enabled} disabled={!canUpdate || mutation.isPending} onCheckedChange={(checked) => setDraft({ ...policy, enabled: checked === true })} /><FieldLabel htmlFor={`policy-${scope}-enabled`}>启用</FieldLabel></Field></CardHeader>
      <CardContent>
      {scope === "system-alerts" ? (
        <div className="mt-5 space-y-4">
          {([
            ["warning", "警告"],
            ["error", "错误"],
            ["critical", "严重"],
            ["recovered", "恢复"],
            ["dailyUnresolved", "每日未恢复提醒"],
          ] as const).map(([key, label]) => <ChannelChoices key={key} label={label} value={policy[key]} disabled={!canUpdate || mutation.isPending} onChange={(value) => setDraft({ ...policy, [key]: value })} />)}
        </div>
      ) : <div className="mt-5"><ChannelChoices label="有效渠道" value={policy.channels} disabled={!canUpdate || mutation.isPending} onChange={(channels) => setDraft({ ...policy, channels })} /></div>}
      <label className="mt-5 grid gap-2 text-sm font-medium">修改原因<Input value={reason} disabled={!canUpdate || mutation.isPending} maxLength={500} onChange={(event) => setReason(event.target.value)} /></label>
      {mutation.isError ? <div className="mt-3"><MutationError error={mutation.error} /></div> : null}
      <Button className="mt-4" disabled={!canUpdate || !reason.trim() || mutation.isPending} onClick={() => mutation.mutate()}><Save />{mutation.isPending ? "正在保存" : "保存策略"}</Button>
      {!canUpdate ? <p className="mt-3 text-xs text-muted-foreground">后端未允许修改，当前策略只读。</p> : null}
      </CardContent>
    </Card>
  )
}

function ChannelChoices({ label, value, disabled, onChange }: { label: string; value: DeliveryChannel[]; disabled: boolean; onChange: (value: DeliveryChannel[]) => void }) {
  return (
    <FieldSet><FieldLegend>{label}</FieldLegend><div className="flex gap-4">{(["WECOM", "EMAIL"] as const).map((channel) => <Field key={channel} orientation="horizontal"><Checkbox id={`${label}-${channel}`} disabled={disabled} checked={value.includes(channel)} onCheckedChange={(checked) => onChange(checked === true ? [...value, channel] : value.filter((item) => item !== channel))} /><FieldLabel htmlFor={`${label}-${channel}`}>{channelLabels[channel]}</FieldLabel></Field>)}</div></FieldSet>
  )
}

type TemplateOperation =
  | { kind: "preview"; template: NotificationTemplate }
  | { kind: "activate"; template: NotificationTemplate }

function TemplatesView({ gateway, onUnauthorized }: { gateway: NotificationGateway; onUnauthorized: () => void }) {
  const queryClient = useQueryClient()
  const [operation, setOperation] = useState<TemplateOperation | null>(null)
  const [reason, setReason] = useState("")
  const [variables, setVariables] = useState("{}")
  const [preview, setPreview] = useState<string | null>(null)
  const query = useQuery({ queryKey: ["notifications", "templates"], queryFn: gateway.loadTemplates })
  const mutation = useMutation({
    mutationFn: async () => {
      if (!operation) return
      if (operation.kind === "activate") {
        await gateway.activateTemplate(operation.template, reason.trim())
      } else {
        const parsed: unknown = JSON.parse(variables)
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("模板变量必须是 JSON 对象")
        const result = await gateway.previewTemplate({ templateType: operation.template.templateType, version: operation.template.version, variables: parsed as Record<string, unknown> })
        setPreview([result.subject, result.text].filter(Boolean).join("\n\n"))
      }
    },
    onSuccess: async () => {
      if (operation?.kind === "activate") {
        close()
        await queryClient.invalidateQueries({ queryKey: ["notifications", "templates"] })
      }
    },
  })
  useUnauthorized(query.error ?? mutation.error, onUnauthorized)
  const grouped = useMemo(() => {
    const groups = new Map<string, NotificationTemplate[]>()
    for (const template of query.data ?? []) groups.set(template.templateType, [...(groups.get(template.templateType) ?? []), template])
    return groups
  }, [query.data])

  function close() {
    if (mutation.isPending) return
    setOperation(null)
    setReason("")
    setVariables("{}")
    setPreview(null)
    mutation.reset()
  }

  if (query.isPending) return <PageState state="loading" title="正在读取模板版本" description="正在加载应用内置的不可变模板版本。" />
  if (query.isError) return <PageState state="error" title="模板版本暂时无法读取" description="已激活模板不会受影响，请稍后重新加载。" action={{ label: "重新加载", onClick: () => void query.refetch() }} error={errorDetails(query.error, "NOTIFICATION_TEMPLATES_FAILED")} />
  if (query.data.length === 0) return <PageState state="empty" title="暂无模板版本" description="应用同步模板后会显示在这里。" />

  return (
    <>
      <section className="grid gap-4" aria-label="通知模板版本">
        {[...grouped.entries()].map(([type, templates]) => (
          <Card key={type} className="grid gap-3 py-4 lg:grid-cols-[16rem_1fr]">
            <div><h2 className="font-mono text-sm font-semibold">{type}</h2><p className="mt-1 text-xs text-muted-foreground">应用内置模板，不支持网页编辑源码。</p></div>
            <div className="space-y-2">
              {templates.map((template) => (
                <div key={template.version} className="flex flex-wrap items-center justify-between gap-3 border-b px-3 py-2 text-sm last:border-b-0">
                  <div className="flex items-center gap-3"><span className="font-mono">{template.version}</span>{template.active ? <span className="flex items-center gap-1 text-primary"><CheckCircle2 className="size-4" />当前启用</span> : <span className="text-muted-foreground">{formatTime(template.createdAt)}</span>}</div>
                  <div className="flex gap-2"><Button size="sm" variant="outline" disabled={!hasAction(template.allowedActions, "PREVIEW")} onClick={() => setOperation({ kind: "preview", template })}><FileText />预览</Button><Button size="sm" disabled={template.active || !hasAction(template.allowedActions, "ACTIVATE")} onClick={() => setOperation({ kind: "activate", template })}><RotateCcw />{template.active ? "已启用" : "启用此版本"}</Button></div>
                </div>
              ))}
            </div>
          </Card>
        ))}
      </section>
      <Dialog open={operation !== null} onOpenChange={(open) => { if (!open) close() }}>
        <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
          <DialogTitle>{operation?.kind === "preview" ? "预览模板" : "启用模板版本"}</DialogTitle>
          <DialogDescription>{operation ? `${operation.template.templateType} · ${operation.template.version}` : ""}</DialogDescription>
          {operation?.kind === "preview" ? <><Field><FieldLabel htmlFor="template-variables">模板变量（JSON）</FieldLabel><Textarea id="template-variables" className="min-h-32 font-mono" value={variables} onChange={(event) => setVariables(event.target.value)} /></Field>{preview ? <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-md bg-muted p-4 text-sm">{preview}</pre> : null}</> : <label className="grid gap-2 text-sm font-medium">操作原因<Input value={reason} maxLength={500} onChange={(event) => setReason(event.target.value)} /></label>}
          {mutation.isError ? <MutationError error={mutation.error} /> : null}
          <DialogFooter><Button variant="outline" disabled={mutation.isPending} onClick={close}>关闭</Button><Button disabled={mutation.isPending || (operation?.kind === "activate" && !reason.trim())} onClick={() => mutation.mutate()}>{mutation.isPending ? "正在处理" : operation?.kind === "preview" ? "生成预览" : "确认启用"}</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}

function StatusBadge({ status }: { status: string }) {
  const danger = ["FAILED", "OUTCOME_UNKNOWN"].includes(status)
  const success = ["DELIVERED", "SENT", "ENABLED"].includes(status)
  const warning = ["PARTIAL", "RETRY_WAIT", "PENDING", "SENDING"].includes(status)
  return <Badge variant={danger ? "destructive" : success ? "default" : warning ? "secondary" : "outline"}>{statusLabels[status] ?? (status === "ENABLED" ? "已启用" : status === "DISABLED" ? "已停用" : status)}</Badge>
}

function MutationError({ error }: { error: unknown }) {
  return <Alert variant="destructive"><AlertTriangle /><AlertDescription><p>{error instanceof Error ? error.message : "操作未完成，请检查后重试。"}</p>{error instanceof ApiError ? <p className="mt-1 text-xs text-muted-foreground">错误码：{error.code}</p> : null}</AlertDescription></Alert>
}

function channelActionLabel(action: ChannelOperation["action"]) {
  if (action === "TEST") return "发送测试"
  if (action === "PROBE") return "连接探测"
  return "重置熔断"
}

function circuitLabel(state: NotificationChannel["circuitState"]) {
  if (state === "CLOSED") return "正常"
  if (state === "OPEN") return "已熔断"
  if (state === "HALF_OPEN") return "探测中"
  return "已停用"
}

function validChannelConfiguration(channel: NotificationChannel) {
  const maxTimeout = channel.channel === "WECOM" ? 15 : 30
  if (
    !Number.isFinite(channel.timeoutSeconds)
    || channel.timeoutSeconds < 1
    || channel.timeoutSeconds > maxTimeout
  ) {
    return false
  }
  if (channel.channel === "WECOM") return true
  return Boolean(
    channel.smtpHost !== null
      && channel.smtpHost.length <= 253
      && channel.smtpPort !== null
      && Number.isInteger(channel.smtpPort)
      && channel.smtpPort >= 1
      && channel.smtpPort <= 65_535
      && (channel.security === "SSL" || channel.security === "STARTTLS"),
  )
}

function useUnauthorized(error: unknown, onUnauthorized: () => void) {
  useEffect(() => {
    if (error instanceof ApiError && error.status === 401) onUnauthorized()
  }, [error, onUnauthorized])
}
