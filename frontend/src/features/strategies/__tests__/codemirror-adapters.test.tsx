import { render, screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import { StrategyCodeEditor, StrategyDiffViewer } from "../codemirror-adapters"

describe("策略 CodeMirror 适配器", () => {
  it("渲染带 Python 支持的可编辑代码区", () => {
    const { container } = render(
      <StrategyCodeEditor
        value="def calculate_targets():\n    return {}"
        onChange={vi.fn()}
        language="python"
        ariaLabel="策略源码"
        height="320px"
      />,
    )

    expect(screen.getByLabelText("策略源码")).toBeInTheDocument()
    expect(container.querySelector(".cm-editor")).toBeInTheDocument()
  })

  it("用合并视图展示两个只读版本", () => {
    const { container } = render(
      <StrategyDiffViewer
        original="return 1"
        modified="return 2"
        language="python"
        originalLabel="版本 1"
        modifiedLabel="版本 2"
      />,
    )

    expect(screen.getByLabelText("版本 1 与 版本 2差异")).toBeInTheDocument()
    expect(screen.getByText("版本 1")).toBeInTheDocument()
    expect(screen.getByText("版本 2")).toBeInTheDocument()
    expect(container.querySelectorAll(".cm-editor")).toHaveLength(2)
    expect(container.querySelectorAll('[contenteditable="false"]')).toHaveLength(2)
  })
})
