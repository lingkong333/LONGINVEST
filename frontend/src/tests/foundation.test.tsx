import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import { AppErrorBoundary } from "@/app/app-error-boundary"
import { FoundationPage } from "@/pages/foundation-page"
import { Button } from "@/shared/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from "@/shared/ui/dialog"
import { FormField } from "@/shared/ui/form-field"
import { Input } from "@/shared/ui/input"
import { PageState } from "@/shared/ui/page-state"
import { DataTable } from "@/shared/ui/table"

describe("公共表单组件", () => {
  it("按钮保留原生语义并能提交表单", async () => {
    const onSubmit = vi.fn((event: React.FormEvent) => event.preventDefault())
    render(
      <form onSubmit={onSubmit}>
        <Button type="submit">保存</Button>
      </form>,
    )

    await userEvent.click(screen.getByRole("button", { name: "保存" }))

    expect(onSubmit).toHaveBeenCalledOnce()
  })

  it("输入框通过表单字段关联标签、说明和错误", () => {
    render(
      <FormField label="股票代码" description="输入沪深北 A 股代码" error="代码格式不正确" htmlFor="symbol">
        <Input id="symbol" aria-invalid />
      </FormField>,
    )

    const input = screen.getByRole("textbox", { name: "股票代码" })
    expect(input).toHaveAccessibleDescription("输入沪深北 A 股代码 代码格式不正确")
    expect(input).toHaveAttribute("aria-invalid", "true")
  })
})

describe("公共覆盖层和表格", () => {
  it("对话框提供标题、说明和键盘关闭", async () => {
    render(
      <Dialog>
        <DialogTrigger asChild>
          <Button>打开确认</Button>
        </DialogTrigger>
        <DialogContent>
          <DialogTitle>确认操作</DialogTitle>
          <DialogDescription>此操作需要再次确认。</DialogDescription>
        </DialogContent>
      </Dialog>,
    )

    await userEvent.click(screen.getByRole("button", { name: "打开确认" }))
    expect(screen.getByRole("dialog", { name: "确认操作" })).toHaveAccessibleDescription("此操作需要再次确认。")
    await userEvent.keyboard("{Escape}")
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument()
  })

  it("数据表包含标题和明确的列头", () => {
    render(
      <DataTable
        caption="任务列表"
        columns={[
          { key: "name", header: "任务" },
          { key: "status", header: "状态" },
        ]}
        rows={[{ id: "1", name: "日线更新", status: "等待中" }]}
      />,
    )

    expect(screen.getByRole("table", { name: "任务列表" })).toBeInTheDocument()
    expect(screen.getAllByRole("columnheader")).toHaveLength(2)
    expect(screen.getByRole("cell", { name: "日线更新" })).toBeInTheDocument()
  })
})

describe("统一页面状态", () => {
  it("加载状态可被辅助技术识别且不会成为无说明旋转图标", () => {
    render(<PageState state="loading" title="正在加载任务" description="正在读取最新任务状态。" />)
    expect(screen.getByRole("status", { name: "正在加载任务" })).toHaveTextContent("正在读取最新任务状态。")
  })

  it("空状态提供清晰说明和可选操作", async () => {
    const onAction = vi.fn()
    render(<PageState state="empty" title="暂无任务" description="当前没有后台任务。" action={{ label: "刷新", onClick: onAction }} />)
    await userEvent.click(screen.getByRole("button", { name: "刷新" }))
    expect(onAction).toHaveBeenCalledOnce()
  })

  it("失败状态展示稳定错误信息并允许重试", async () => {
    const onRetry = vi.fn()
    render(
      <PageState
        state="error"
        title="任务加载失败"
        description="请稍后重试。"
        error={{ code: "DEPENDENCY_UNAVAILABLE", requestId: "req_01abc" }}
        action={{ label: "重试", onClick: onRetry }}
      />,
    )
    expect(screen.getByText("DEPENDENCY_UNAVAILABLE")).toBeInTheDocument()
    expect(screen.getByText("req_01abc")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "重试" }))
    expect(onRetry).toHaveBeenCalledOnce()
  })
})

describe("应用错误边界和入口", () => {
  it("未知渲染错误被隔离且不展示堆栈", () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined)
    const Broken = () => {
      throw new Error("private stack detail")
    }

    render(
      <AppErrorBoundary>
        <Broken />
      </AppErrorBoundary>,
    )

    expect(screen.getByRole("alert")).toHaveTextContent("页面出现异常")
    expect(screen.queryByText("private stack detail")).not.toBeInTheDocument()
    consoleError.mockRestore()
  })

  it("重新尝试会清除错误边界并重新渲染子内容", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined)
    let broken = true
    const Recoverable = () => {
      if (broken) {
        throw new Error("temporary")
      }
      return <p>页面已恢复</p>
    }

    render(
      <AppErrorBoundary onReset={() => { broken = false }}>
        <Recoverable />
      </AppErrorBoundary>,
    )
    await userEvent.click(screen.getByRole("button", { name: "重新尝试" }))
    expect(screen.getByText("页面已恢复")).toBeInTheDocument()
    consoleError.mockRestore()
  })

  it("入口只显示中性的基础工程状态", () => {
    render(<FoundationPage />)
    expect(screen.getByRole("heading", { name: "LongInvest 前端基础工程" })).toBeInTheDocument()
    expect(screen.getByText("基础组件与应用运行环境已就绪")).toBeInTheDocument()
  })
})
