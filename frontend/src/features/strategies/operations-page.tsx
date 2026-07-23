import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ChartNoAxesCombined, Code2, Plus, RefreshCw } from "lucide-react"
import { useState } from "react"

import { Button } from "@/shared/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/shared/ui/dialog"
import { Input } from "@/shared/ui/input"
import { PageState } from "@/shared/ui/page-state"

import { StrategyBacktestWorkspace } from "./backtest-workspace"
import { strategyEditorComponents } from "./editor-components"
import { createStrategyApi } from "./gateway"
import { StrategyWorkspace } from "./strategy-workspace"
import type { StrategyApi, StrategyEditorComponents } from "./types"

type StrategyView = "editor" | "backtest"

const strategyStatusLabels: Record<string, string> = {
  DRAFT: "草稿",
  VALIDATING: "验证中",
  VALIDATED: "已验证",
  PUBLISHING: "发布中",
  PUBLISHED: "已发布",
  PUBLISH_FAILED: "发布失败",
  ARCHIVED: "已归档",
}

export function StrategyOperationsPage({
  api = createStrategyApi(),
  editorComponents = strategyEditorComponents,
  initialView = "editor",
}: {
  api?: StrategyApi
  editorComponents?: StrategyEditorComponents
  initialView?: StrategyView
}) {
  const queryClient = useQueryClient()
  const [selectedStrategyId, setSelectedStrategyId] = useState<string | null>(null)
  const [view, setView] = useState<StrategyView>(initialView)
  const [createOpen, setCreateOpen] = useState(false)
  const [name, setName] = useState("")
  const [reason, setReason] = useState("")
  const strategiesQuery = useQuery({
    queryKey: ["strategies", "list"],
    queryFn: () => api.listStrategies(),
  })
  const createMutation = useMutation({
    mutationFn: () => api.createStrategy(name.trim(), reason.trim()),
    onSuccess: async (created) => {
      setSelectedStrategyId(created.id)
      setCreateOpen(false)
      setName("")
      setReason("")
      await queryClient.invalidateQueries({ queryKey: ["strategies", "list"] })
    },
  })

  if (strategiesQuery.isPending) {
    return <PageState state="loading" title="正在加载策略" description="正在读取策略列表和可用操作。" />
  }
  if (strategiesQuery.isError || !strategiesQuery.data) {
    return <PageState state="error" title="策略列表无法加载" description="请检查网络后重试。" action={{ label: "重试", onClick: () => void strategiesQuery.refetch() }} />
  }

  const selectedId = strategiesQuery.data.items.some((item) => item.id === selectedStrategyId)
    ? selectedStrategyId
    : strategiesQuery.data.items[0]?.id ?? null

  return <section className="mx-auto grid w-full max-w-[96rem] gap-4 p-4 lg:grid-cols-[16rem_minmax(0,1fr)] lg:p-6">
    <aside className="border-r border-border pr-0 lg:pr-4">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h1 className="m-0 text-lg font-semibold">策略</h1>
        <div className="flex gap-1">
          <Button type="button" size="icon-sm" variant="ghost" title="刷新策略" onClick={() => void strategiesQuery.refetch()} disabled={strategiesQuery.isFetching}><RefreshCw aria-hidden="true" /></Button>
          {strategiesQuery.data.canCreate ? <Button type="button" size="icon-sm" title="新建策略" onClick={() => setCreateOpen(true)}><Plus aria-hidden="true" /></Button> : null}
        </div>
      </div>
      {strategiesQuery.data.items.length ? <nav aria-label="策略列表" className="grid gap-1">
        {strategiesQuery.data.items.map((strategy) => <Button key={strategy.id} type="button" variant={strategy.id === selectedId ? "secondary" : "ghost"} className="h-auto justify-start px-3 py-2 text-left" onClick={() => setSelectedStrategyId(strategy.id)}>
          <span className="min-w-0"><span className="block truncate">{strategy.name}</span><span className="block text-xs text-muted-foreground">{strategyStatusLabels[strategy.status] ?? strategy.status}</span></span>
        </Button>)}
      </nav> : <p className="text-sm text-muted-foreground">暂无策略。</p>}
    </aside>
    <main className="min-w-0">
      {selectedId ? <>
        <div role="tablist" aria-label="策略视图" className="mb-2 flex w-fit border border-border bg-muted p-1">
          <Button type="button" role="tab" aria-selected={view === "editor"} size="sm" variant={view === "editor" ? "default" : "ghost"} onClick={() => setView("editor")}><Code2 aria-hidden="true" />编辑</Button>
          <Button type="button" role="tab" aria-selected={view === "backtest"} size="sm" variant={view === "backtest" ? "default" : "ghost"} onClick={() => setView("backtest")}><ChartNoAxesCombined aria-hidden="true" />回测</Button>
        </div>
        {view === "editor"
          ? <StrategyWorkspace strategyId={selectedId} api={api} editorComponents={editorComponents} />
          : <StrategyBacktestWorkspace strategyId={selectedId} api={api} />}
      </> : <PageState state="empty" title="暂无策略" description={strategiesQuery.data.canCreate ? "新建策略后可以编辑和运行样本外回测。" : "当前没有可查看的策略。"} />}
    </main>
    <Dialog open={createOpen} onOpenChange={(open) => { if (!createMutation.isPending) setCreateOpen(open) }}>
      <DialogContent showCloseButton={false} onEscapeKeyDown={(event) => { if (createMutation.isPending) event.preventDefault() }} onPointerDownOutside={(event) => { if (createMutation.isPending) event.preventDefault() }}>
        <DialogTitle>新建策略</DialogTitle>
        <DialogDescription>创建空白草稿后进入编辑器。</DialogDescription>
        <label className="grid gap-2 text-sm font-medium">策略名称<Input maxLength={100} value={name} onChange={(event) => setName(event.target.value)} /></label>
        <label className="grid gap-2 text-sm font-medium">创建原因<Input maxLength={200} value={reason} onChange={(event) => setReason(event.target.value)} /></label>
        {createMutation.isError ? <p role="alert" className="text-sm text-destructive">创建失败，请检查名称和当前权限后重试。</p> : null}
        <div className="flex justify-end gap-2"><Button type="button" variant="outline" disabled={createMutation.isPending} onClick={() => setCreateOpen(false)}>取消</Button><Button type="button" disabled={!name.trim() || !reason.trim() || createMutation.isPending} onClick={() => createMutation.mutate()}>{createMutation.isPending ? "创建中" : "确认创建"}</Button></div>
      </DialogContent>
    </Dialog>
  </section>
}
