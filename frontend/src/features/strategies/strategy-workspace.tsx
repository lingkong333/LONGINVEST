import Editor from "@monaco-editor/react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Archive, CheckCircle2, Clipboard, FlaskConical, History, RotateCcw, Rocket, Save } from "lucide-react"
import { useEffect, useMemo, useRef, useState } from "react"
import { Controller, useWatch } from "react-hook-form"
import { z } from "zod"

import { useZodForm } from "@/shared/forms/use-zod-form"
import { Button } from "@/shared/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/shared/ui/dialog"
import { FormField } from "@/shared/ui/form-field"
import { Input } from "@/shared/ui/input"
import { PageState } from "@/shared/ui/page-state"

import {
  isSaveConflict,
  type DraftSaveInput,
  type SaveConflict,
  type StrategyAction,
  type StrategyApi,
  type StrategyDraft,
  type StrategyRunResult,
} from "./types"

const schemaTypes = new Set(["null", "boolean", "object", "array", "number", "string", "integer"])

function isSchemaNode(value: unknown): boolean {
  if (typeof value === "boolean") return true
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false
  const schema = value as Record<string, unknown>
  if ("type" in schema) {
    const types = Array.isArray(schema.type) ? schema.type : [schema.type]
    if (!types.length || types.some((type) => typeof type !== "string" || !schemaTypes.has(type))) return false
  }
  if ("properties" in schema) {
    if (typeof schema.properties !== "object" || schema.properties === null || Array.isArray(schema.properties)) return false
    if (Object.values(schema.properties).some((child) => !isSchemaNode(child))) return false
  }
  if ("required" in schema && (!Array.isArray(schema.required) || schema.required.some((item) => typeof item !== "string"))) return false
  if ("items" in schema && !isSchemaNode(schema.items)) return false
  if ("additionalProperties" in schema && typeof schema.additionalProperties !== "boolean" && !isSchemaNode(schema.additionalProperties)) return false
  if ("enum" in schema && (!Array.isArray(schema.enum) || schema.enum.length === 0)) return false
  return true
}

function isJsonSchema(value: string): boolean {
  try {
    const parsed: unknown = JSON.parse(value)
    return isSchemaNode(parsed)
  } catch {
    return false
  }
}

const draftSchema = z.object({
  name: z.string().trim().min(1, "请输入策略名称"),
  description: z.string().trim().min(1, "请输入策略说明"),
  sourceCode: z.string().min(1, "请输入 Python 策略源码"),
  parameterSchema: z.string().superRefine((value, context) => {
    try { JSON.parse(value) } catch {
      context.addIssue({ code: "custom", message: "请输入合法的 JSON" })
      return
    }
    if (!isJsonSchema(value)) context.addIssue({ code: "custom", message: "请输入合法的 JSON Schema" })
  }),
})

type DraftForm = z.input<typeof draftSchema>
type ReasonAction = StrategyAction | "restore"

type DraftField = keyof DraftForm
type ConflictChoice = "server" | "local" | "custom"

interface ConflictResolution {
  choice: ConflictChoice
  custom: string
}

interface ConflictState extends SaveConflict {
  base: StrategyDraft
  local: DraftForm
  resolutions: Record<DraftField, ConflictResolution>
}

function toDraftInput(values: DraftForm, expectedVersion: number): DraftSaveInput {
  return { ...values, expectedVersion }
}

function draftDefaults(draft: StrategyDraft): DraftForm {
  return {
    name: draft.name,
    description: draft.description,
    sourceCode: draft.sourceCode,
    parameterSchema: draft.parameterSchema,
  }
}

function OperationButton(props: {
  icon: React.ReactNode
  label: string
  onClick: () => void
  disabled?: boolean
}) {
  return <Button type="button" variant="secondary" onClick={props.onClick} disabled={props.disabled}>{props.icon}<span>{props.label}</span></Button>
}

function newIdempotencyKey(): string {
  return globalThis.crypto?.randomUUID?.() ?? `restore-${Date.now()}`
}

function containsConflictMarker(value: string): boolean {
  return /^(<{7}|={7}|>{7})/m.test(value)
}

const draftFields: Array<{ field: DraftField; label: string }> = [
  { field: "name", label: "策略名称" },
  { field: "description", label: "策略说明" },
  { field: "parameterSchema", label: "参数 JSON Schema" },
  { field: "sourceCode", label: "Python 策略源码" },
]

function cloneDraft(draft: StrategyDraft): StrategyDraft {
  return structuredClone(draft)
}

function conflictResolutions(server: StrategyDraft): ConflictState["resolutions"] {
  const values = draftDefaults(server)
  return Object.fromEntries(draftFields.map(({ field }) => [field, { choice: "server", custom: values[field] }])) as ConflictState["resolutions"]
}

const versionStatusLabels = {
  PUBLISHING: "发布中",
  PUBLISHED: "已发布",
  PUBLISH_FAILED: "发布失败",
  ARCHIVED: "已归档",
} as const

export function StrategyWorkspace({ strategyId, api }: { strategyId: string; api: StrategyApi }) {
  const queryClient = useQueryClient()
  const [expectedVersion, setExpectedVersion] = useState<number | null>(null)
  const baseDraftRef = useRef<StrategyDraft | null>(null)
  const [conflict, setConflict] = useState<ConflictState | null>(null)
  const [mergeConfirmed, setMergeConfirmed] = useState(false)
  const [reasonAction, setReasonAction] = useState<ReasonAction | null>(null)
  const [revisionToRestore, setRevisionToRestore] = useState<string | null>(null)
  const [restoreKey, setRestoreKey] = useState("")
  const [reason, setReason] = useState("")
  const [actionMessage, setActionMessage] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [actionPending, setActionPending] = useState(false)
  const [validationResult, setValidationResult] = useState<StrategyRunResult | undefined>()
  const [testResult, setTestResult] = useState<StrategyRunResult | undefined>()
  const [lastActionResult, setLastActionResult] = useState<{ action: StrategyAction; result: StrategyRunResult } | null>(null)
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null)

  const draftQuery = useQuery({ queryKey: ["strategies", strategyId, "draft"], queryFn: () => api.getDraft(strategyId) })
  const revisionsQuery = useQuery({ queryKey: ["strategies", strategyId, "revisions"], queryFn: () => api.listRevisions(strategyId) })
  const versionsQuery = useQuery({ queryKey: ["strategies", strategyId, "versions"], queryFn: () => api.listVersions(strategyId) })
  const form = useZodForm(draftSchema, { defaultValues: { name: "", description: "", sourceCode: "", parameterSchema: "{}" } })
  const watchedDraft = useWatch({ control: form.control })
  const activeVersion = expectedVersion ?? draftQuery.data?.version ?? 0

  useEffect(() => {
    if (!draftQuery.data || form.formState.isDirty) return
    form.reset(draftDefaults(draftQuery.data))
    baseDraftRef.current = cloneDraft(draftQuery.data)
  }, [draftQuery.data, form])

  const saveMutation = useMutation({
    mutationFn: (input: DraftSaveInput) => api.saveDraft(strategyId, input),
    onSuccess: (saved) => {
      queryClient.setQueryData(["strategies", strategyId, "draft"], saved)
      form.reset(draftDefaults(saved))
      setExpectedVersion(saved.version)
      baseDraftRef.current = cloneDraft(saved)
      setConflict(null)
      setMergeConfirmed(false)
      setActionMessage("草稿已保存")
    },
    onError: (error, input) => {
      if (!isSaveConflict(error)) return
      const local = { name: input.name, description: input.description, sourceCode: input.sourceCode, parameterSchema: input.parameterSchema }
      setConflict({
        ...error,
        base: cloneDraft(baseDraftRef.current ?? error.current),
        local,
        resolutions: conflictResolutions(error.current),
      })
    },
  })

  const saveCurrent = async (): Promise<StrategyDraft> => {
    const parsed = await draftSchema.parseAsync(form.getValues())
    return saveMutation.mutateAsync(toDraftInput(parsed, activeVersion))
  }

  useEffect(() => {
    if (!form.formState.isDirty || conflict || saveMutation.isPending) return
    const timer = window.setTimeout(() => {
      const parsed = draftSchema.safeParse(form.getValues())
      if (parsed.success) void saveMutation.mutateAsync(toDraftInput(parsed.data, activeVersion)).catch(() => undefined)
    }, 30_000)
    return () => window.clearTimeout(timer)
  }, [activeVersion, conflict, form, form.formState.isDirty, saveMutation, watchedDraft])

  const validationFresh = !form.formState.isDirty
    && (validationResult ?? draftQuery.data?.validationResult)?.status === "SUCCEEDED"
    && (validationResult ?? draftQuery.data?.validationResult)?.sourceVersion === activeVersion
  const testFresh = !form.formState.isDirty
    && (testResult ?? draftQuery.data?.testResult)?.status === "SUCCEEDED"
    && (testResult ?? draftQuery.data?.testResult)?.sourceVersion === activeVersion
  const allowed = (action: StrategyAction) => draftQuery.data?.allowedActions.includes(action) === true
  const selectedVersion = versionsQuery.data?.find((version) => version.id === selectedVersionId)
  const versionDiff = useMemo(() => {
    if (!selectedVersion?.sourceCode) return null
    return { current: form.getValues("sourceCode"), published: selectedVersion.sourceCode }
  }, [form, selectedVersion])

  if (draftQuery.isPending) return <PageState state="loading" title="正在加载策略草稿" description="正在获取服务器上的最新草稿。" />
  if (draftQuery.isError || !draftQuery.data) return <PageState state="error" title="策略草稿无法加载" description="请检查网络后重试。" action={{ label: "重试", onClick: () => void draftQuery.refetch() }} />

  const saveNow = form.handleSubmit(async () => {
    setActionError(null)
    setActionMessage(null)
    try { await saveCurrent() } catch { /* mutation state displays the failure */ }
  })

  const openAction = (action: ReasonAction, revisionId?: string) => {
    setActionError(null)
    setActionMessage(null)
    setReason("")
    setReasonAction(action)
    setRevisionToRestore(revisionId ?? null)
    setRestoreKey(action === "restore" ? newIdempotencyKey() : "")
  }

  const runAction = async () => {
    if (!reasonAction || !reason.trim() || actionPending) return
    setActionPending(true)
    setActionError(null)
    try {
      if (reasonAction === "restore") {
        if (!revisionToRestore) return
        const restored = await api.restoreRevision(strategyId, revisionToRestore, reason.trim(), restoreKey)
        queryClient.setQueryData(["strategies", strategyId, "draft"], restored)
        form.reset(draftDefaults(restored))
        setExpectedVersion(restored.version)
        baseDraftRef.current = cloneDraft(restored)
        setActionMessage("回滚成功")
      } else {
        let actionDraft = draftQuery.data
        if (["validate", "test", "publish", "archive"].includes(reasonAction) && form.formState.isDirty) {
          try { await saveCurrent() } catch (error) {
            if (isSaveConflict(error)) setReasonAction(null)
            else setActionError("草稿保存失败，后续操作已中止。请检查网络后重试。")
            return
          }
          actionDraft = queryClient.getQueryData<StrategyDraft>(["strategies", strategyId, "draft"]) ?? actionDraft
        }
        if (!actionDraft.allowedActions.includes(reasonAction)) {
          setActionError("服务器已不允许执行此操作，操作已中止。")
          return
        }
        if (reasonAction === "publish") {
          const freshValidation = actionDraft.validationResult?.status === "SUCCEEDED" && actionDraft.validationResult.sourceVersion === actionDraft.version
          const freshTest = actionDraft.testResult?.status === "SUCCEEDED" && actionDraft.testResult.sourceVersion === actionDraft.version
          if (!freshValidation || !freshTest) {
            setActionError("当前草稿的验证或测试已失效，发布已中止。")
            return
          }
        }
        const calls = {
          validate: api.validateDraft,
          test: api.testDraft,
          publish: api.publishDraft,
          archive: api.archiveStrategy,
        }
        const result = await calls[reasonAction](strategyId, reason.trim())
        if (reasonAction === "publish" || reasonAction === "archive") setLastActionResult({ action: reasonAction, result })
        if (reasonAction === "validate") setValidationResult(result)
        if (reasonAction === "test") setTestResult(result)
        if (result.status === "FAILED" || result.status === "CANCELED") {
          setActionError(result.summary ?? (result.status === "FAILED" ? "操作执行失败。" : "操作已取消。"))
          setReasonAction(null)
          return
        }
        const completedLabels = { validate: "验证已完成", test: "测试已完成", publish: "发布已完成", archive: "归档已完成" }
        const submittedLabels = { validate: "验证已提交", test: "测试已提交", publish: "发布已提交", archive: "归档已提交" }
        setActionMessage(result.status === "SUCCEEDED" ? completedLabels[reasonAction] : submittedLabels[reasonAction])
      }
      setReasonAction(null)
      setReason("")
      await Promise.all([draftQuery.refetch(), revisionsQuery.refetch(), versionsQuery.refetch()])
    } catch {
      setActionError(reasonAction === "restore" ? "回滚失败，请重试。" : "操作失败，请查看原因后重试。")
    } finally {
      setActionPending(false)
    }
  }

  const submitMerge = async () => {
    if (!conflict || !mergeConfirmed) return
    const server = draftDefaults(conflict.current)
    const merged = Object.fromEntries(draftFields.map(({ field }) => {
      const resolution = conflict.resolutions[field]
      const value = resolution.choice === "server" ? server[field] : resolution.choice === "local" ? conflict.local[field] : resolution.custom
      return [field, value]
    })) as DraftForm
    if (containsConflictMarker(merged.sourceCode)) return
    try { await saveMutation.mutateAsync(toDraftInput(merged, conflict.current.version)) } catch { /* keep conflict dialog open */ }
  }

  return (
    <section className="mx-auto grid w-full max-w-7xl gap-5 p-4 lg:grid-cols-[minmax(0,1fr)_18rem] lg:p-6">
      <form className="min-w-0 space-y-4" onSubmit={saveNow}>
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-border pb-4">
          <div><p className="text-sm font-medium text-muted-foreground">策略工作台</p><h1 className="m-0 text-2xl font-semibold">{draftQuery.data.name}</h1></div>
          <div className="flex flex-wrap gap-2">
            <OperationButton icon={<Save size={16} />} label={saveMutation.isPending ? "保存中" : "保存"} onClick={saveNow} disabled={saveMutation.isPending || conflict !== null} />
            <OperationButton icon={<CheckCircle2 size={16} />} label="验证" onClick={() => openAction("validate")} disabled={!allowed("validate") || actionPending || conflict !== null} />
            <OperationButton icon={<FlaskConical size={16} />} label="测试" onClick={() => openAction("test")} disabled={!allowed("test") || actionPending || conflict !== null} />
            <OperationButton icon={<Rocket size={16} />} label="发布" onClick={() => openAction("publish")} disabled={!allowed("publish") || !validationFresh || !testFresh || actionPending || conflict !== null} />
            <OperationButton icon={<Archive size={16} />} label="归档" onClick={() => openAction("archive")} disabled={!allowed("archive") || actionPending || conflict !== null} />
          </div>
        </header>
        {form.formState.isDirty ? <p className="text-sm text-amber-700">源码已变化，需要重新验证和测试</p> : null}
        <div className="grid gap-4 md:grid-cols-2">
          <FormField control={form.control} name="name" label="策略名称">{({ field }) => <Input {...field} />}</FormField>
          <FormField control={form.control} name="description" label="策略说明">{({ field }) => <Input {...field} />}</FormField>
        </div>
        <Controller control={form.control} name="sourceCode" render={({ field }) => <div className="overflow-hidden border border-border bg-card"><label className="block border-b border-border px-3 py-2 text-sm font-medium">Python 策略源码</label><Editor height="34rem" language="python" value={field.value} onChange={field.onChange} options={{ ariaLabel: "Python 策略源码", automaticLayout: true, minimap: { enabled: false }, lineNumbers: "on", find: { addExtraSpaceOnTop: true }, bracketPairColorization: { enabled: true } }} /></div>} />
        <FormField control={form.control} name="parameterSchema" label="参数 JSON Schema">{({ field }) => <textarea className="min-h-32 w-full border border-input bg-card p-3 font-mono text-sm" {...field} />}</FormField>
        {saveMutation.isError && !conflict ? <p role="alert" className="text-sm text-destructive">保存失败，请检查网络后重试。</p> : null}
        {actionMessage ? <p role="status" className="text-sm text-primary">{actionMessage}</p> : null}
        {lastActionResult ? <section aria-label="最近操作结果" className="border border-border p-3"><h2 className="text-sm font-semibold">最近操作结果</h2><p role={lastActionResult.result.status === "FAILED" || lastActionResult.result.status === "CANCELED" ? "alert" : "status"} className={lastActionResult.result.status === "FAILED" || lastActionResult.result.status === "CANCELED" ? "text-destructive" : ""}>{lastActionResult.result.summary ?? `状态：${lastActionResult.result.status}`}</p>{lastActionResult.result.details?.map((detail) => <p key={detail}>{detail}</p>)}</section> : null}
        {(validationResult ?? draftQuery.data.validationResult) ? <section aria-label="验证运行结果"><h2 className="text-sm font-semibold">验证运行结果</h2><p role={(validationResult ?? draftQuery.data.validationResult)?.status === "FAILED" || (validationResult ?? draftQuery.data.validationResult)?.status === "CANCELED" ? "alert" : "status"}>{(validationResult ?? draftQuery.data.validationResult)?.summary}</p>{(validationResult ?? draftQuery.data.validationResult)?.details?.map((detail) => <p key={detail}>{detail}</p>)}</section> : null}
        {(testResult ?? draftQuery.data.testResult) ? <section aria-label="测试运行结果"><h2 className="text-sm font-semibold">测试运行结果</h2><p role={(testResult ?? draftQuery.data.testResult)?.status === "FAILED" || (testResult ?? draftQuery.data.testResult)?.status === "CANCELED" ? "alert" : "status"}>{(testResult ?? draftQuery.data.testResult)?.summary}</p>{(testResult ?? draftQuery.data.testResult)?.details?.map((detail) => <p key={detail}>{detail}</p>)}</section> : null}
      </form>
      <aside className="space-y-5 border-l border-border pl-0 lg:pl-5">
        <section>
          <div className="flex items-center gap-2"><History size={16} /><h2 className="text-sm font-semibold">草稿历史</h2></div>
          {revisionsQuery.isPending ? <p className="text-sm text-muted-foreground">正在加载草稿历史……</p> : revisionsQuery.isError ? <p role="alert" className="text-sm text-destructive">草稿历史加载失败，请重试。</p> : revisionsQuery.data?.length ? <ol className="space-y-2">{revisionsQuery.data.map((revision) => <li key={revision.id} className="border border-border p-2 text-sm"><span>修订 {revision.revisionNo}</span><Button type="button" variant="ghost" onClick={() => openAction("restore", revision.id)}><RotateCcw size={14} />应用回滚</Button></li>)}</ol> : <p className="text-sm text-muted-foreground">暂无历史草稿。</p>}
        </section>
        <section>
          <h2 className="text-sm font-semibold">发布版本</h2>
          {versionsQuery.isPending ? <p className="text-sm text-muted-foreground">正在加载版本……</p> : versionsQuery.isError ? <p role="alert" className="text-sm text-destructive">版本列表加载失败，请重试。</p> : versionsQuery.data?.length ? <ul className="space-y-2">{versionsQuery.data.map((version) => <li key={version.id} className="border border-border p-2 text-sm"><span>版本 {version.versionNo} · {versionStatusLabels[version.status]}</span><Button type="button" variant="ghost" onClick={() => setSelectedVersionId(version.id)}>查看差异</Button></li>)}</ul> : <p className="text-sm text-muted-foreground">暂无发布版本。</p>}
          {versionDiff ? <div className="mt-3 grid gap-2"><strong>当前草稿</strong><pre className="max-h-32 overflow-auto bg-muted p-2">{versionDiff.current}</pre><strong>所选版本</strong><pre className="max-h-32 overflow-auto bg-muted p-2">{versionDiff.published}</pre></div> : null}
        </section>
      </aside>
      <Dialog open={conflict !== null} onOpenChange={() => undefined}>
        <DialogContent showCloseButton={false} onEscapeKeyDown={(event) => event.preventDefault()} onPointerDownOutside={(event) => event.preventDefault()}>
          <DialogTitle>保存冲突</DialogTitle>
          <DialogDescription>服务器草稿已更新。自动保存已暂停，请选择保留服务器版本或完成三方人工合并。</DialogDescription>
          {conflict ? <div className="grid gap-3 text-sm">
            {draftFields.map(({ field, label }) => {
              const base = draftDefaults(conflict.base)[field]
              const local = conflict.local[field]
              const server = draftDefaults(conflict.current)[field]
              const resolution = conflict.resolutions[field]
              const customLabel = field === "sourceCode" ? "人工合并结果" : `${label}人工合并结果`
              return <fieldset key={field} className="grid gap-2 border border-border p-3"><legend className="font-semibold">{label}</legend><div className="grid gap-2 sm:grid-cols-3"><div><strong>基础版本</strong><pre className="max-h-24 overflow-auto bg-muted p-2">{base}</pre></div><div><strong>本地版本</strong><pre className="max-h-24 overflow-auto bg-muted p-2">{local}</pre></div><div><strong>服务器版本</strong><pre className="max-h-24 overflow-auto bg-muted p-2">{server}</pre></div></div><div className="flex flex-wrap gap-3"><label><input type="radio" name={`merge-${field}`} checked={resolution.choice === "server"} onChange={() => setConflict({ ...conflict, resolutions: { ...conflict.resolutions, [field]: { ...resolution, choice: "server" } } })} />{label}采用服务器版本</label><label><input type="radio" name={`merge-${field}`} checked={resolution.choice === "local"} onChange={() => setConflict({ ...conflict, resolutions: { ...conflict.resolutions, [field]: { ...resolution, choice: "local" } } })} />{label}采用本地版本</label><label><input type="radio" name={`merge-${field}`} checked={resolution.choice === "custom"} onChange={() => setConflict({ ...conflict, resolutions: { ...conflict.resolutions, [field]: { ...resolution, choice: "custom" } } })} />人工编辑</label></div><textarea aria-label={customLabel} className="min-h-20 border border-input p-2 font-mono" value={resolution.custom} onChange={(event) => setConflict({ ...conflict, resolutions: { ...conflict.resolutions, [field]: { choice: "custom", custom: event.target.value } } })} /></fieldset>
            })}
            {containsConflictMarker(conflict.resolutions.sourceCode.choice === "local" ? conflict.local.sourceCode : conflict.resolutions.sourceCode.choice === "server" ? conflict.current.sourceCode : conflict.resolutions.sourceCode.custom) ? <p role="alert" className="text-destructive">仍有未解决的冲突标记，不能提交。</p> : null}
            <label className="flex gap-2"><input type="checkbox" checked={mergeConfirmed} onChange={(event) => setMergeConfirmed(event.target.checked)} />我已核对三方内容</label>
            <div className="flex flex-wrap gap-2">
              <Button type="button" variant="secondary" onClick={() => void navigator.clipboard?.writeText(JSON.stringify(conflict.local, null, 2))}><Clipboard size={16} />复制本地内容</Button>
              <Button type="button" variant="secondary" onClick={() => { form.reset(draftDefaults(conflict.current)); setExpectedVersion(conflict.current.version); baseDraftRef.current = cloneDraft(conflict.current); setConflict(null) }}>放弃本地修改并采用服务器版本</Button>
              <Button type="button" onClick={() => void submitMerge()} disabled={!mergeConfirmed || containsConflictMarker(conflict.resolutions.sourceCode.choice === "local" ? conflict.local.sourceCode : conflict.resolutions.sourceCode.choice === "server" ? conflict.current.sourceCode : conflict.resolutions.sourceCode.custom) || saveMutation.isPending}>提交人工合并</Button>
            </div>
          </div> : null}
        </DialogContent>
      </Dialog>
      <Dialog open={reasonAction !== null} onOpenChange={(open) => { if (!open && !actionPending) setReasonAction(null) }}>
        <DialogContent showCloseButton={false} onEscapeKeyDown={(event) => { if (actionPending) event.preventDefault() }}>
          <DialogTitle>{reasonAction === "restore" ? "确认应用回滚" : "确认策略操作"}</DialogTitle>
          <DialogDescription>这是重要操作，请填写原因后确认。</DialogDescription>
          <label className="grid gap-2 text-sm font-medium">操作原因<Input value={reason} onChange={(event) => setReason(event.target.value)} /></label>
          {actionError ? <p role="alert" className="text-sm text-destructive">{actionError}</p> : null}
          {!actionPending ? <Button type="button" variant="secondary" onClick={() => setReasonAction(null)}>取消</Button> : null}
          <Button type="button" onClick={() => void runAction()} disabled={!reason.trim() || actionPending}>{actionPending ? "处理中" : "确认执行"}</Button>
        </DialogContent>
      </Dialog>
    </section>
  )
}
