import { StrategyWorkspace, type StrategyApi, type StrategyEditorComponents } from "@/features/strategies"

/** Task 7 supplies the generated API adapter and route parameters. */
export function StrategyWorkspacePage({ strategyId, api, editorComponents }: { strategyId: string; api: StrategyApi; editorComponents: StrategyEditorComponents }) {
  return <StrategyWorkspace strategyId={strategyId} api={api} editorComponents={editorComponents} />
}
