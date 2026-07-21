import { StrategyBacktestWorkspace, type StrategyApi } from "@/features/strategies"

/** Task 7 supplies the generated API adapter and route parameters. */
export function StrategyBacktestPage({ strategyId, api }: { strategyId: string; api: StrategyApi }) {
  return <StrategyBacktestWorkspace strategyId={strategyId} api={api} />
}
