import Editor from "@monaco-editor/react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Archive, CheckCircle2, Clipboard, FlaskConical, History, RotateCcw, Rocket, Save } from "lucide-react"
import { useEffect, useState } from "react"
import { Controller } from "react-hook-form"
import { z } from "zod"

import { useZodForm } from "@/shared/forms/use-zod-form"
import { Button } from "@/shared/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/shared/ui/dialog"
import { FormField } from "@/shared/ui/form-field"
import { Input } from "@/shared/ui/input"
import { PageState } from "@/shared/ui/page-state"

import { isSaveConflict, type DraftSaveInput, type SaveConflict, type StrategyApi, type StrategyDraft } from "./types"

const draftSchema = z.object({
  name: z.string().trim().min(1, "Strategy name is required"),
  description: z.string().trim().min(1, "Description is required"),
  sourceCode: z.string().min(1, "Python source is required"),
  parameterSchema: z.string().min(2, "Parameter schema is required"),
})

type DraftForm = z.input<typeof draftSchema>

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

function OperationButton({ icon, label, onClick, disabled }: { icon: React.ReactNode; label: string; onClick: () => void; disabled?: boolean }) {
  return <Button type="button" variant="secondary" onClick={onClick} disabled={disabled}>{icon}<span>{label}</span></Button>
}

export function StrategyWorkspace({ strategyId, api }: { strategyId: string; api: StrategyApi }) {
  const queryClient = useQueryClient()
  const [expectedVersion, setExpectedVersion] = useState<number | null>(null)
  const [conflict, setConflict] = useState<SaveConflict | null>(null)
  const [reasonAction, setReasonAction] = useState<"validate" | "test" | "publish" | "archive" | null>(null)
  const [reason, setReason] = useState("")
  const [actionMessage, setActionMessage] = useState<string | null>(null)
  const draftQuery = useQuery({ queryKey: ["strategies", strategyId, "draft"], queryFn: () => api.getDraft(strategyId) })
  const revisionsQuery = useQuery({ queryKey: ["strategies", strategyId, "revisions"], queryFn: () => api.listRevisions(strategyId) })
  const versionsQuery = useQuery({ queryKey: ["strategies", strategyId, "versions"], queryFn: () => api.listVersions(strategyId) })
  const form = useZodForm(draftSchema, { defaultValues: { name: "", description: "", sourceCode: "", parameterSchema: "{}" } })
  const activeVersion = expectedVersion ?? draftQuery.data?.version ?? 0

  useEffect(() => {
    if (!draftQuery.data || form.formState.isDirty) return
    form.reset(draftDefaults(draftQuery.data))
  }, [draftQuery.data, form])

  const saveMutation = useMutation({
    mutationFn: (input: DraftSaveInput) => api.saveDraft(strategyId, input),
    onSuccess: (saved) => {
      queryClient.setQueryData(["strategies", strategyId, "draft"], saved)
      form.reset(draftDefaults(saved))
      setExpectedVersion(saved.version)
      setConflict(null)
    },
    onError: (error) => {
      if (isSaveConflict(error)) setConflict(error)
    },
  })
  const actionMutation = useMutation({
    mutationFn: async ({ action, actionReason }: { action: NonNullable<typeof reasonAction>; actionReason: string }) => {
      const actions = { validate: api.validateDraft, test: api.testDraft, publish: api.publishDraft, archive: api.archiveStrategy }
      await actions[action](strategyId, actionReason)
      return action
    },
    onSuccess: async (action) => {
      const labels = { validate: "Validation", test: "Test", publish: "Publication", archive: "Archive" }
      setActionMessage(`${labels[action]} requested`)
      setReasonAction(null)
      setReason("")
      await Promise.all([revisionsQuery.refetch(), versionsQuery.refetch()])
    },
  })

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (!form.formState.isDirty || conflict || saveMutation.isPending) return
      const parsed = draftSchema.safeParse(form.getValues())
      if (parsed.success) saveMutation.mutate(toDraftInput(parsed.data, activeVersion))
    }, 30_000)
    return () => window.clearInterval(timer)
  }, [activeVersion, conflict, form, saveMutation])

  if (draftQuery.isPending) return <PageState state="loading" title="Loading strategy draft" description="Loading the latest server draft." />
  if (draftQuery.isError || !draftQuery.data) return <PageState state="error" title="Strategy draft is unavailable" description="Try loading the draft again." action={{ label: "Retry", onClick: () => void draftQuery.refetch() }} />

  const saveNow = form.handleSubmit((values) => saveMutation.mutate(toDraftInput(values, activeVersion)))
  const runAction = () => {
    if (!reasonAction || !reason.trim()) return
    actionMutation.mutate({ action: reasonAction, actionReason: reason.trim() })
  }
  const restore = async (revisionId: string) => {
    const restored = await api.restoreRevision(strategyId, revisionId, "Restore draft revision")
    queryClient.setQueryData(["strategies", strategyId, "draft"], restored)
    form.reset(draftDefaults(restored))
    setExpectedVersion(restored.version)
  }

  return (
    <section className="mx-auto grid w-full max-w-7xl gap-5 p-4 lg:grid-cols-[minmax(0,1fr)_18rem] lg:p-6">
      <form className="min-w-0 space-y-4" onSubmit={saveNow}>
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-border pb-4">
          <div><p className="text-sm font-medium text-muted-foreground">Strategy workspace</p><h1 className="m-0 text-2xl font-semibold">{draftQuery.data.name}</h1></div>
          <div className="flex flex-wrap gap-2"><OperationButton icon={<Save size={16} />} label={saveMutation.isPending ? "Saving" : "Save"} onClick={saveNow} disabled={saveMutation.isPending || conflict !== null} /><OperationButton icon={<CheckCircle2 size={16} />} label="Validate" onClick={() => setReasonAction("validate")} disabled={actionMutation.isPending} /><OperationButton icon={<FlaskConical size={16} />} label="Test" onClick={() => setReasonAction("test")} disabled={actionMutation.isPending} /><OperationButton icon={<Rocket size={16} />} label="Publish" onClick={() => setReasonAction("publish")} disabled={actionMutation.isPending} /><OperationButton icon={<Archive size={16} />} label="Archive" onClick={() => setReasonAction("archive")} disabled={actionMutation.isPending} /></div>
        </header>
        <div className="grid gap-4 md:grid-cols-2">
          <FormField control={form.control} name="name" label="Strategy name">{({ field }) => <Input {...field} />}</FormField>
          <FormField control={form.control} name="description" label="Description">{({ field }) => <Input {...field} />}</FormField>
        </div>
        <Controller control={form.control} name="sourceCode" render={({ field }) => <div className="overflow-hidden border border-border bg-card"><label className="block border-b border-border px-3 py-2 text-sm font-medium">Python source</label><Editor height="34rem" language="python" value={field.value} onChange={field.onChange} options={{ automaticLayout: true, minimap: { enabled: false }, lineNumbers: "on", find: { addExtraSpaceOnTop: true }, bracketPairColorization: { enabled: true } }} /></div>} />
        <FormField control={form.control} name="parameterSchema" label="Parameter JSON schema">{({ field }) => <textarea className="min-h-32 w-full border border-input bg-card p-3 font-mono text-sm" {...field} />}</FormField>
        {saveMutation.isError && !conflict ? <p role="alert" className="text-sm text-destructive">Save failed. Check your connection and retry.</p> : null}
        {actionMutation.isError ? <p role="alert" className="text-sm text-destructive">The strategy action failed. Review the service response and retry.</p> : null}
        {actionMessage ? <p role="status" className="text-sm text-primary">{actionMessage}</p> : null}
      </form>
      <aside className="space-y-5 border-l border-border pl-0 lg:pl-5">
        <section><div className="flex items-center gap-2"><History size={16} /><h2 className="text-sm font-semibold">Draft history</h2></div>{revisionsQuery.isPending ? <p className="text-sm text-muted-foreground">Loading revisions...</p> : revisionsQuery.data?.length ? <ol className="space-y-2">{revisionsQuery.data.map((revision) => <li key={revision.id} className="border border-border p-2 text-sm"><span>Revision {revision.revisionNo}</span><Button type="button" variant="ghost" onClick={() => void restore(revision.id)}><RotateCcw size={14} />Restore</Button></li>)}</ol> : <p className="text-sm text-muted-foreground">No earlier revisions.</p>}</section>
        <section><h2 className="text-sm font-semibold">Published versions</h2>{versionsQuery.isPending ? <p className="text-sm text-muted-foreground">Loading versions...</p> : versionsQuery.data?.length ? <ul className="space-y-2">{versionsQuery.data.map((version) => <li key={version.id} className="border border-border p-2 text-sm">v{version.versionNo} · {version.status}</li>)}</ul> : <p className="text-sm text-muted-foreground">No published versions.</p>}</section>
      </aside>
      <Dialog open={conflict !== null} onOpenChange={(open) => { if (!open) setConflict(null) }}><DialogContent><DialogTitle>Save conflict</DialogTitle><DialogDescription>The server draft changed before this save. Automatic saving is paused until you decide how to continue.</DialogDescription>{conflict ? <div className="grid gap-3 text-sm"><pre className="max-h-32 overflow-auto bg-muted p-2">{conflict.current.sourceCode}</pre><div className="flex flex-wrap gap-2"><Button type="button" variant="secondary" onClick={() => void navigator.clipboard?.writeText(form.getValues("sourceCode"))}><Clipboard size={16} />Copy local source</Button><Button type="button" variant="secondary" onClick={() => { form.reset(draftDefaults(conflict.current)); setExpectedVersion(conflict.current.version); setConflict(null) }}>Discard local changes</Button><Button type="button" onClick={() => { setExpectedVersion(conflict.current.version); setConflict(null); const parsed = draftSchema.safeParse(form.getValues()); if (parsed.success) saveMutation.mutate(toDraftInput(parsed.data, conflict.current.version)) }}>Merge and retry</Button></div></div> : null}</DialogContent></Dialog>
      <Dialog open={reasonAction !== null} onOpenChange={(open) => { if (!open) setReasonAction(null) }}><DialogContent><DialogTitle>{reasonAction ? `${reasonAction[0].toUpperCase()}${reasonAction.slice(1)} strategy` : "Strategy action"}</DialogTitle><DialogDescription>Provide a reason before continuing with this high-impact action.</DialogDescription><label className="grid gap-2 text-sm font-medium">Reason<Input value={reason} onChange={(event) => setReason(event.target.value)} /></label><Button type="button" onClick={runAction} disabled={!reason.trim() || actionMutation.isPending}>{actionMutation.isPending ? "Submitting" : "Confirm"}</Button></DialogContent></Dialog>
    </section>
  )
}
