import { python } from "@codemirror/lang-python"
import CodeMirror from "@uiw/react-codemirror"
import CodeMirrorMerge from "react-codemirror-merge"
import { useTheme } from "next-themes"

import type { CodeEditorProps, DiffViewerProps } from "./types"

const Original = CodeMirrorMerge.Original
const Modified = CodeMirrorMerge.Modified

export function StrategyCodeEditor({ value, onChange, ariaLabel, height }: CodeEditorProps) {
  const { resolvedTheme } = useTheme()
  return (
    <CodeMirror
      aria-label={ariaLabel}
      value={value}
      height={height}
      theme={resolvedTheme === "dark" ? "dark" : "light"}
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
  const { resolvedTheme } = useTheme()
  const theme = resolvedTheme === "dark" ? "dark" : "light"
  return (
    <div className="grid gap-3" aria-label={`${originalLabel} 与 ${modifiedLabel}差异`}>
      <div className="grid grid-cols-2 gap-3 text-xs font-medium text-muted-foreground">
        <span>{originalLabel}</span>
        <span>{modifiedLabel}</span>
      </div>
      <CodeMirrorMerge orientation="a-b" theme={theme} className="overflow-hidden rounded-md border">
        <Original value={original} extensions={[python()]} readOnly editable={false} />
        <Modified value={modified} extensions={[python()]} readOnly editable={false} />
      </CodeMirrorMerge>
    </div>
  )
}
