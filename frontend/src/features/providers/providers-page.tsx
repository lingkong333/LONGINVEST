import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  Activity,
  Gauge,
  Pencil,
  RefreshCw,
  RotateCcw,
  Search,
  ShieldCheck,
} from "lucide-react"
import { useState } from "react"

import { providerGateway } from "@/features/providers/gateway"
import type {
  ProviderAction,
  ProviderCircuit,
  ProviderGateway,
  ProviderSettingsInput,
  ProviderSummary,
  QuoteDiagnostic,
} from "@/features/providers/types"
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
import { Input } from "@/shared/ui/input"
import { PageState } from "@/shared/ui/page-state"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/shared/ui/table"

const providerLabels = {
  EASTMONEY: "东方财富",
  SINA: "新浪行情",
} as const

const capabilityLabels: Record<string, string> = {
  REALTIME_QUOTE_BATCH: "批量实时行情",
  SECURITY_MASTER: "股票主数据",
  DAILY_BAR_UNADJUSTED: "不复权日线",
  HISTORICAL_DAILY_QFQ: "前复权历史",
}

const statusLabels: Record<string, string> = {
  HEALTHY: "健康",
  DEGRADED: "降级",
  CIRCUIT_OPEN: "熔断中",
  HALF_OPEN: "试探恢复",
  DISABLED: "已停用",
  UNKNOWN: "未知",
  CLOSED: "正常",
  OPEN: "已熔断",
}

function labelCapability(value: string) {
  return capabilityLabels[value] ?? value
}

function hasAction(actions: ProviderAction[], action: ProviderAction) {
  return actions.includes(action)
}

function errorDetails(error: unknown, fallback: string) {
  return error instanceof ApiError
    ? { code: error.code, requestId: error.requestId }
    : { code: fallback }
}

function formatTime(value: string | null) {
  if (!value) return "暂无"
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    dateStyle: "short",
    timeStyle: "medium",
  }).format(new Date(value))
}

export function ProvidersPage({
  gateway = providerGateway,
}: {
  gateway?: ProviderGateway
}) {
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState<ProviderSummary | null>(null)
  const [settings, setSettings] = useState<ProviderSettingsInput["settings"] | null>(null)
  const [circuitAction, setCircuitAction] = useState<{
    circuit: ProviderCircuit
    action: "PROBE" | "RESET"
  } | null>(null)
  const [reason, setReason] = useState("")
  const [diagnosticOpen, setDiagnosticOpen] = useState(false)
  const [symbolsText, setSymbolsText] = useState("")
  const [diagnosticResult, setDiagnosticResult] = useState<QuoteDiagnostic | null>(null)

  const providers = useQuery({
    queryKey: ["providers", "list"],
    queryFn: gateway.loadProviders,
  })
  const circuits = useQuery({
    queryKey: ["providers", "circuits"],
    queryFn: gateway.loadCircuits,
  })
  const updateMutation = useMutation({
    mutationFn: () => gateway.updateSettings({
      provider: editing!,
      settings: settings!,
      reason: reason.trim(),
    }),
    onSuccess: async () => {
      closeDialog()
      await queryClient.invalidateQueries({ queryKey: ["providers"] })
    },
  })
  const circuitMutation = useMutation({
    mutationFn: () => gateway.runCircuitAction({
      ...circuitAction!,
      reason: reason.trim(),
    }),
    onSuccess: async () => {
      closeDialog()
      await queryClient.invalidateQueries({ queryKey: ["providers"] })
    },
  })
  const diagnosticMutation = useMutation({
    mutationFn: () => gateway.runQuoteDiagnostics(parsedSymbols, reason.trim()),
    onSuccess: async (result) => {
      setDiagnosticResult(result)
      setReason("")
      await queryClient.invalidateQueries({ queryKey: ["providers"] })
    },
  })
  const parsedSymbols = Array.from(new Set(
    symbolsText.split(/[\s,，]+/).map((item) => item.trim().toUpperCase()).filter(Boolean),
  ))
  const canDiagnose = providers.data?.some((provider) => (
    hasAction(provider.allowedActions, "QUOTE_DIAGNOSTICS")
  )) === true

  function openSettings(provider: ProviderSummary) {
    const first = provider.capabilities[0]
    if (!first) return
    setEditing(provider)
    setSettings({
      enabled: first.enabled,
      priority: first.priority,
      concurrency: first.concurrency,
      ratePerSecond: first.ratePerSecond,
      timeoutSeconds: first.timeoutSeconds,
      autoSwitch: first.autoSwitch,
    })
    setReason("")
    updateMutation.reset()
  }

  function closeDialog() {
    if (updateMutation.isPending || circuitMutation.isPending) return
    setEditing(null)
    setSettings(null)
    setCircuitAction(null)
    setReason("")
    updateMutation.reset()
    circuitMutation.reset()
  }

  if (providers.isPending) {
    return <PageState state="loading" title="正在读取数据源" description="正在加载能力、健康状态和熔断记录。" />
  }
  if (providers.isError) {
    return <PageState state="error" title="数据源暂时无法读取" description="后台采集不受此页面影响，请稍后重新加载。" action={{ label: "重新加载", onClick: () => void providers.refetch() }} error={errorDetails(providers.error, "PROVIDER_LIST_FAILED")} />
  }
  if (providers.data.length === 0) {
    return <PageState state="empty" title="暂无已注册数据源" description="系统启动并注册行情来源后会显示在这里。" />
  }

  return (
    <main className="mx-auto flex w-full max-w-[1500px] flex-col gap-6 px-4 py-6 lg:px-8">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b pb-5">
        <div>
          <h1 className="text-3xl font-semibold">数据源管理</h1>
          <p className="mt-2 text-sm text-muted-foreground">查看行情能力、运行健康和隔离熔断状态。</p>
        </div>
        <Button
          variant="outline"
          disabled={!canDiagnose}
          title={canDiagnose ? "比较各数据源的标准化行情" : "服务器未允许行情诊断"}
          onClick={() => {
            setDiagnosticOpen(true)
            setDiagnosticResult(null)
            setReason("")
            diagnosticMutation.reset()
          }}
        >
          <Search data-icon="inline-start" />行情诊断
        </Button>
      </header>

      <section className="grid gap-4 xl:grid-cols-2" aria-label="数据源列表">
        {providers.data.map((provider) => (
          <Card key={provider.code}>
            <CardHeader className="flex-row items-start justify-between">
              <div className="flex items-center gap-3">
                <span className="rounded-md bg-muted p-2"><Activity className="size-5" /></span>
                <div>
                  <CardTitle>{providerLabels[provider.code]}</CardTitle>
                  <CardDescription>配置版本 v{provider.version}</CardDescription>
                </div>
              </div>
              <Button
                size="icon-sm"
                variant="outline"
                aria-label={`编辑 ${providerLabels[provider.code]} 配置`}
                title={hasAction(provider.allowedActions, "UPDATE_SETTINGS") ? "编辑受限运行参数" : "服务器未允许修改"}
                disabled={!hasAction(provider.allowedActions, "UPDATE_SETTINGS") || provider.capabilities.length === 0}
                onClick={() => openSettings(provider)}
              >
                <Pencil data-icon="icon" />
              </Button>
            </CardHeader>
            <CardContent>
              <Table>
                <TableHeader>
                  <TableRow><TableHead>能力</TableHead><TableHead>状态</TableHead><TableHead>优先级</TableHead><TableHead>并发</TableHead><TableHead>每秒速率</TableHead><TableHead>超时</TableHead></TableRow>
                </TableHeader>
                <TableBody>
                  {provider.capabilities.map((capability) => (
                    <TableRow key={capability.capability}>
                      <TableCell>{labelCapability(capability.capability)}</TableCell>
                      <TableCell><Badge variant={capability.enabled ? "default" : "secondary"}>{capability.enabled ? "启用" : "停用"}</Badge></TableCell>
                      <TableCell>{capability.priority}</TableCell>
                      <TableCell>{capability.concurrency}</TableCell>
                      <TableCell>{capability.ratePerSecond}</TableCell>
                      <TableCell>{capability.timeoutSeconds} 秒</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            <HealthPanel provider={provider} gateway={gateway} />
            </CardContent>
          </Card>
        ))}
      </section>

      <Card aria-label="熔断状态">
        <CardHeader>
          <CardTitle className="flex items-center gap-3"><ShieldCheck className="size-5" />熔断状态</CardTitle>
          <CardDescription>按数据源和能力分别隔离。</CardDescription>
        </CardHeader>
        <CardContent>
        {circuits.isPending ? <p className="mt-5 text-sm text-muted-foreground">正在读取熔断状态…</p> : circuits.isError ? (
          <Alert variant="destructive"><AlertDescription className="flex items-center justify-between gap-3"><span>熔断状态读取失败，数据源概览仍可使用。</span><Button size="sm" variant="outline" onClick={() => void circuits.refetch()}><RefreshCw data-icon="inline-start" />重新加载</Button></AlertDescription></Alert>
        ) : circuits.data.length === 0 ? <p className="mt-5 text-sm text-muted-foreground">暂无熔断记录</p> : (
            <Table>
              <TableHeader><TableRow><TableHead>数据源</TableHead><TableHead>能力</TableHead><TableHead>状态</TableHead><TableHead>连续失败</TableHead><TableHead>打开时间</TableHead><TableHead className="text-right">操作</TableHead></TableRow></TableHeader>
              <TableBody>
                {circuits.data.map((circuit) => (
                  <TableRow key={circuit.id}>
                    <TableCell>{providerLabels[circuit.providerCode]}</TableCell><TableCell>{labelCapability(circuit.capability)}</TableCell><TableCell><Badge variant="outline">{statusLabels[circuit.state] ?? circuit.state}</Badge></TableCell><TableCell>{circuit.consecutiveFailures}</TableCell><TableCell>{formatTime(circuit.openedAt)}</TableCell>
                    <TableCell><div className="flex justify-end gap-2">
                      <Button size="icon-sm" variant="outline" aria-label={`探测 ${providerLabels[circuit.providerCode]} ${labelCapability(circuit.capability)}`} title={hasAction(circuit.allowedActions, "PROBE") ? "执行安全探测" : "服务器未允许探测"} disabled={!hasAction(circuit.allowedActions, "PROBE")} onClick={() => { setCircuitAction({ circuit, action: "PROBE" }); setReason(""); circuitMutation.reset() }}><Gauge data-icon="icon" /></Button>
                      <Button size="icon-sm" variant="outline" aria-label={`重置 ${providerLabels[circuit.providerCode]} ${labelCapability(circuit.capability)}`} title={hasAction(circuit.allowedActions, "RESET") ? "进入试探恢复并探测" : "服务器未允许重置"} disabled={!hasAction(circuit.allowedActions, "RESET")} onClick={() => { setCircuitAction({ circuit, action: "RESET" }); setReason(""); circuitMutation.reset() }}><RotateCcw data-icon="icon" /></Button>
                    </div></TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
        )}
        </CardContent>
      </Card>

      <Dialog open={editing !== null || circuitAction !== null} onOpenChange={(open) => { if (!open) closeDialog() }}>
        <DialogContent>
          <DialogTitle>{editing ? `编辑${providerLabels[editing.code]}配置` : circuitAction?.action === "RESET" ? "确认重置熔断" : "确认执行探测"}</DialogTitle>
          <DialogDescription>{editing ? "这些受限参数会统一应用到该数据源当前支持的全部能力。" : "探测使用系统固定安全股票，不写入正式行情，也不触发信号。"}</DialogDescription>
          {editing && settings ? <SettingsFields settings={settings} onChange={setSettings} /> : null}
          <label className="grid gap-2 text-sm font-medium">操作原因<Input maxLength={255} value={reason} onChange={(event) => setReason(event.target.value)} /></label>
          {updateMutation.isError || circuitMutation.isError ? <p role="alert" className="text-sm text-destructive">{(updateMutation.error ?? circuitMutation.error as Error).message}</p> : null}
          <DialogFooter><Button variant="outline" disabled={updateMutation.isPending || circuitMutation.isPending} onClick={closeDialog}>取消</Button><Button disabled={!reason.trim() || updateMutation.isPending || circuitMutation.isPending} onClick={() => editing ? updateMutation.mutate() : circuitMutation.mutate()}>{updateMutation.isPending || circuitMutation.isPending ? "正在提交" : "确认执行"}</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={diagnosticOpen} onOpenChange={(open) => { if (!diagnosticMutation.isPending) setDiagnosticOpen(open) }}>
        <DialogContent>
          <DialogTitle>行情来源诊断</DialogTitle>
          <DialogDescription>仅比较各来源的标准化结果，不改变正式优先级、不写入行情、不触发信号。</DialogDescription>
          <label className="grid gap-2 text-sm font-medium">股票代码<Input placeholder="例如：600000.SH，000001.SZ" value={symbolsText} onChange={(event) => setSymbolsText(event.target.value)} /></label>
          <label className="grid gap-2 text-sm font-medium">操作原因<Input maxLength={255} value={reason} onChange={(event) => setReason(event.target.value)} /></label>
          {diagnosticMutation.isError ? <p role="alert" className="text-sm text-destructive">{diagnosticMutation.error.message}</p> : null}
          {diagnosticResult ? <Alert aria-label="诊断结果"><AlertDescription>{diagnosticResult.comparisons.map((item) => <p key={item.symbol}>{item.symbol}：{item.status === "MATCH" ? "来源一致" : item.status === "CONFLICT" ? "存在差异" : "来源不完整"}</p>)}</AlertDescription></Alert> : null}
          <DialogFooter><Button variant="outline" disabled={diagnosticMutation.isPending} onClick={() => setDiagnosticOpen(false)}>关闭</Button><Button disabled={parsedSymbols.length === 0 || parsedSymbols.length > 100 || !reason.trim() || diagnosticMutation.isPending} onClick={() => diagnosticMutation.mutate()}>{diagnosticMutation.isPending ? "正在诊断" : "确认诊断"}</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </main>
  )
}

function HealthPanel({ provider, gateway }: { provider: ProviderSummary; gateway: ProviderGateway }) {
  const query = useQuery({ queryKey: ["providers", provider.code, "health"], queryFn: () => gateway.loadHealth(provider.code) })
  if (query.isPending) return <p className="mt-5 text-sm text-muted-foreground">正在读取健康状态…</p>
  if (query.isError) return <Alert className="mt-5" variant="destructive"><AlertDescription>健康状态读取失败，配置仍可查看。</AlertDescription></Alert>
  if (query.data.length === 0) return <p className="mt-5 text-sm text-muted-foreground">暂无健康观测</p>
  return <div className="mt-5 border-t pt-4"><h3 className="text-sm font-semibold">运行健康</h3><ul className="mt-3 grid gap-2 sm:grid-cols-2">{query.data.map((health) => <li className="rounded-md bg-muted/40 p-3 text-sm" key={health.capability}><div className="flex justify-between gap-3"><span>{labelCapability(health.capability)}</span><Badge variant="outline">{statusLabels[health.status] ?? health.status}</Badge></div><p className="mt-2 text-xs text-muted-foreground">连续失败 {health.consecutiveFailures} 次 · P95 {health.p95LatencyMs === null ? "暂无" : `${health.p95LatencyMs} ms`}</p></li>)}</ul></div>
}

function SettingsFields({ settings, onChange }: { settings: ProviderSettingsInput["settings"]; onChange: (value: ProviderSettingsInput["settings"]) => void }) {
  const numberField = (key: "priority" | "concurrency" | "ratePerSecond" | "timeoutSeconds", value: string) => onChange({ ...settings, [key]: Number(value) })
  return <div className="grid gap-3 sm:grid-cols-2">
    <label className="flex items-center gap-2 text-sm font-medium"><Checkbox checked={settings.enabled} onCheckedChange={(checked) => onChange({ ...settings, enabled: checked === true })} />启用数据源</label>
    <label className="flex items-center gap-2 text-sm font-medium"><Checkbox checked={settings.autoSwitch} onCheckedChange={(checked) => onChange({ ...settings, autoSwitch: checked === true })} />允许自动切换</label>
    <label className="grid gap-2 text-sm font-medium">优先级<Input type="number" min={0} max={20} value={settings.priority} onChange={(event) => numberField("priority", event.target.value)} /></label>
    <label className="grid gap-2 text-sm font-medium">并发上限<Input type="number" min={1} max={32} value={settings.concurrency} onChange={(event) => numberField("concurrency", event.target.value)} /></label>
    <label className="grid gap-2 text-sm font-medium">每秒速率<Input type="number" min={0.1} max={100} step={0.1} value={settings.ratePerSecond} onChange={(event) => numberField("ratePerSecond", event.target.value)} /></label>
    <label className="grid gap-2 text-sm font-medium">超时（秒）<Input type="number" min={0.1} max={60} step={0.1} value={settings.timeoutSeconds} onChange={(event) => numberField("timeoutSeconds", event.target.value)} /></label>
  </div>
}
