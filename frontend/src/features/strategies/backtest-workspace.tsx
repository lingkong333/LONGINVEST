import { useMutation, useQuery } from "@tanstack/react-query"
import { useState } from "react"
import { z } from "zod"

import { useZodForm } from "@/shared/forms/use-zod-form"
import { Button } from "@/shared/ui/button"
import { FormField } from "@/shared/ui/form-field"
import { Input } from "@/shared/ui/input"
import { PageState } from "@/shared/ui/page-state"
import { DataTable } from "@/shared/ui/table"

import type { HoldoutBacktestInput, HoldoutBacktestResult, StrategyApi } from "./types"

const holdoutSchema = z.object({
  securitySymbol: z.string().regex(/^\d{6}\.(SH|SZ|BJ)$/, "Use a six-digit A-share symbol, for example 600000.SH"),
  trainingStartDate: z.string().min(1, "Training start date is required"),
  trainingEndDate: z.string().min(1, "Training end date is required"),
  testStartDate: z.string().min(1, "Test start date is required"),
  testEndDate: z.string().min(1, "Test end date is required"),
}).superRefine((value, context) => {
  if (value.trainingEndDate && value.testStartDate && value.trainingEndDate >= value.testStartDate) context.addIssue({ code: "custom", path: ["testStartDate"], message: "Test period must start after training ends" })
  if (value.trainingStartDate && value.trainingEndDate && value.trainingStartDate > value.trainingEndDate) context.addIssue({ code: "custom", path: ["trainingEndDate"], message: "Training end date must not precede its start" })
  if (value.testStartDate && value.testEndDate && value.testStartDate > value.testEndDate) context.addIssue({ code: "custom", path: ["testEndDate"], message: "Test end date must not precede its start" })
})

function BacktestResult({ result }: { result: HoldoutBacktestResult }) {
  if (result.status === "FAILED") return <PageState state="error" title="Backtest failed" description={result.failureMessage ?? "The backtest did not complete."} />
  if (result.status !== "SUCCEEDED") return <PageState state="loading" title="Backtest is running" description="The frozen target simulation is still processing." />
  return <div className="space-y-6"><section><h2 className="text-lg font-semibold">Frozen target prices</h2><div className="grid grid-cols-2 gap-2 sm:grid-cols-4">{result.frozenTargets.map((target) => <div key={target.label} className="border border-border bg-card p-3"><p className="m-0 text-xs text-muted-foreground">{target.label}</p><strong>{target.price}</strong></div>)}</div></section><section><h2 className="text-lg font-semibold">Target adjustments</h2>{result.adjustments.length ? <DataTable caption="Target adjustments" columns={[{ key: "eventDate", header: "Event date" }, { key: "factor", header: "Factor" }, { key: "source", header: "Source" }]} rows={result.adjustments.map((item, index) => ({ ...item, id: `${item.eventDate}-${index}` }))} /> : <p className="text-sm text-muted-foreground">No target adjustments were required.</p>}</section><section><h2 className="text-lg font-semibold">Trades</h2><DataTable caption="Holdout trades" columns={[{ key: "date", header: "Date" }, { key: "direction", header: "Direction" }, { key: "price", header: "Price" }, { key: "quantity", header: "Quantity" }]} rows={result.trades.map((trade, index) => ({ ...trade, id: `${trade.date}-${index}` }))} /></section><section><h2 className="text-lg font-semibold">Metrics</h2><dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">{result.metrics.map((metric) => <div key={metric.label} className="border border-border bg-card p-3"><dt className="text-xs text-muted-foreground">{metric.label}</dt><dd className="m-0 font-semibold">{metric.value}</dd></div>)}</dl></section></div>
}

export function StrategyBacktestWorkspace({ strategyId, api }: { strategyId: string; api: StrategyApi }) {
  const [backtestId, setBacktestId] = useState<string | null>(null)
  const form = useZodForm(holdoutSchema, { defaultValues: { securitySymbol: "", trainingStartDate: "", trainingEndDate: "", testStartDate: "", testEndDate: "" } })
  const createMutation = useMutation({ mutationFn: (input: HoldoutBacktestInput) => api.createHoldoutBacktest(input), onSuccess: (result) => setBacktestId(result.id) })
  const resultQuery = useQuery({ queryKey: ["strategies", strategyId, "holdout", backtestId], queryFn: () => api.getHoldoutBacktest(backtestId ?? ""), enabled: backtestId !== null, refetchInterval: (query) => query.state.data?.status === "RUNNING" || query.state.data?.status === "PENDING" ? 3_000 : false })
  const submit = form.handleSubmit((values) => createMutation.mutate({ strategyId, ...values }))

  return <section className="mx-auto grid w-full max-w-5xl gap-6 p-4 lg:p-6"><header><p className="text-sm font-medium text-muted-foreground">Single-security fixed-target holdout</p><h1 className="m-0 text-2xl font-semibold">Out-of-sample backtest</h1><p className="mt-2 max-w-2xl text-sm text-muted-foreground">The strategy receives training data only. Test-period data stays outside the sandbox.</p></header><form className="grid gap-4 rounded border border-border bg-card p-4 md:grid-cols-2" onSubmit={submit}><FormField control={form.control} name="securitySymbol" label="Security symbol">{({ field }) => <Input placeholder="600000.SH" {...field} />}</FormField><div className="hidden md:block" /><FormField control={form.control} name="trainingStartDate" label="Training start date">{({ field }) => <Input type="date" {...field} />}</FormField><FormField control={form.control} name="trainingEndDate" label="Training end date">{({ field }) => <Input type="date" {...field} />}</FormField><FormField control={form.control} name="testStartDate" label="Test start date">{({ field }) => <Input type="date" {...field} />}</FormField><FormField control={form.control} name="testEndDate" label="Test end date">{({ field }) => <Input type="date" {...field} />}</FormField><div className="md:col-span-2"><Button type="submit" disabled={createMutation.isPending}>{createMutation.isPending ? "Starting backtest" : "Run holdout backtest"}</Button>{createMutation.isError ? <p role="alert" className="mt-2 text-sm text-destructive">Unable to start the backtest. Try again.</p> : null}</div></form>{resultQuery.isError ? <PageState state="error" title="Backtest result is unavailable" description="Try loading the result again." action={{ label: "Retry", onClick: () => void resultQuery.refetch() }} /> : resultQuery.data ? <BacktestResult result={resultQuery.data} /> : null}</section>
}
