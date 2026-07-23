import { python } from "@codemirror/lang-python"
import CodeMirror from "@uiw/react-codemirror"
import CodeMirrorMerge from "react-codemirror-merge"

import type { CodeEditorProps, DiffViewerProps } from "./types"

const Original = CodeMirrorMerge.Original
const Modified = CodeMirrorMerge.Modified

export function StrategyCodeEditor({ value, onChange, ariaLabel, height }: CodeEditorProps) {
  return (
    <CodeMirror
      aria-label={ariaLabel}
      value={value}
      height={height}
      extensions={[python()]}
      onChange={onChange}
      basicSetup={{
        bracketMatching: true,
        closeBrackets: true,
        highlightActiveLine: true,
        highlightActiveLineGutter: true,
        lineNumbers: true,
        searchKeymap: true,
      }}
    />
  )
}

export function StrategyDiffViewer({
  original,
  modified,
  originalLabel,
  modifiedLabel,
}: DiffViewerProps) {
  return (
    <div className="grid gap-3" aria-label={`${originalLabel} 与 ${modifiedLabel}差异`}>
      <div className="grid grid-cols-2 gap-3 text-xs font-medium text-muted-foreground">
        <span>{originalLabel}</span>
        <span>{modifiedLabel}</span>
      </div>
      <CodeMirrorMerge orientation="a-b" className="overflow-hidden rounded-md border">
        <Original value={original} extensions={[python()]} readOnly editable={false} />
        <Modified value={modified} extensions={[python()]} readOnly editable={false} />
      </CodeMirrorMerge>
    </div>
  )
}
