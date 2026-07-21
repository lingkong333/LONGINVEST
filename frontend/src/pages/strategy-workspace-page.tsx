import { StrategyWorkspace, type StrategyApi } from "@/features/strategies"

/** Task 7 supplies the generated API adapter and route parameters. */
export function StrategyWorkspacePage({ strategyId, api }: { strategyId: string; api: StrategyApi }) {
  return <StrategyWorkspace strategyId={strategyId} api={api} />
}
