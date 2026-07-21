import { StrategyCodeEditor, StrategyDiffViewer } from "./codemirror-adapters"
import type { StrategyEditorComponents } from "./types"

export const strategyEditorComponents: StrategyEditorComponents = {
  CodeEditor: StrategyCodeEditor,
  DiffViewer: StrategyDiffViewer,
}
