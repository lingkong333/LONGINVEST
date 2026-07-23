import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"
import { z } from "zod"
import { createMemoryRouter, RouterProvider } from "react-router-dom"

import { AppErrorBoundary } from "@/app/app-error-boundary"
import { RouteErrorPage } from "@/app/route-error-page"
import { FoundationPage } from "@/pages/foundation-page"
import { ApiError } from "@/shared/api/client"
import { useZodForm } from "@/shared/forms/use-zod-form"
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

  it("RHF 与 Zod 校验失败时自动关联错误并标记输入无效", async () => {
    const schema = z.object({
      symbol: z.string().regex(/^\d{6}$/, "代码格式不正确"),
    })
    const onSubmit = vi.fn()
    const TestForm = () => {
      const form = useZodForm(schema, { defaultValues: { symbol: "" } })
      return (
        <form onSubmit={form.handleSubmit(onSubmit)}>
          <FormField
            control={form.control}
            name="symbol"
            label="股票代码"
            description="输入沪深北 A 股代码"
          >
            {({ field }) => <Input {...field} />}
          </FormField>
          <Button type="submit">提交</Button>
        </form>
      )
    }
    render(<TestForm />)

    await userEvent.click(screen.getByRole("button", { name: "提交" }))

    const input = screen.getByRole("textbox", { name: "股票代码" })
    expect(input).toHaveAccessibleDescription("输入沪深北 A 股代码 代码格式不正确")
    expect(input).toHaveAttribute("aria-invalid", "true")

    await userEvent.type(input, "600000")
    await userEvent.click(screen.getByRole("button", { name: "提交" }))
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith({ symbol: "600000" }, expect.anything()))
    expect(input).toHaveAttribute("aria-invalid", "false")
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
  it("渲染错误显示稳定诊断并可复制，不展示堆栈", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined)
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } })
    const Broken = () => {
      throw new ApiError("private stack detail", {
        code: "DEPENDENCY_UNAVAILABLE",
        requestId: "req_boundary",
        status: 503,
      })
    }

    render(
      <AppErrorBoundary>
        <Broken />
      </AppErrorBoundary>,
    )

    expect(screen.getByRole("alert")).toHaveTextContent("页面出现异常")
    expect(screen.getByText("DEPENDENCY_UNAVAILABLE")).toBeInTheDocument()
    expect(screen.getByText("req_boundary")).toBeInTheDocument()
    expect(screen.queryByText("private stack detail")).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "复制诊断信息" }))
    expect(writeText).toHaveBeenCalledWith("错误码: DEPENDENCY_UNAVAILABLE\n请求标识: req_boundary")
    consoleError.mockRestore()
  })

  it("未知渲染错误生成稳定客户端诊断标识", () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined)
    const Broken = () => { throw new Error("unknown") }

    render(<AppErrorBoundary><Broken /></AppErrorBoundary>)

    expect(screen.getByText("UNEXPECTED_CLIENT_ERROR")).toBeInTheDocument()
    expect(screen.getByText(/^web_/)).toBeInTheDocument()
    consoleError.mockRestore()
  })

  it("路由错误入口显示并复制服务端诊断信息", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } })
    let loaderShouldFail = true
    const loader = vi.fn(() => {
      if (!loaderShouldFail) {
        return null
      }
      throw new ApiError("认证后端不可用", {
        code: "AUTH_BACKEND_UNAVAILABLE",
        requestId: "req_route",
        status: 503,
      })
    })
    const router = createMemoryRouter([
      {
        path: "/",
        loader,
        element: <p>页面已恢复</p>,
        errorElement: <RouteErrorPage />,
      },
    ])

    render(<RouterProvider router={router} future={{ v7_startTransition: true }} />)

    expect(await screen.findByText("AUTH_BACKEND_UNAVAILABLE")).toBeInTheDocument()
    expect(screen.getByText("req_route")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "复制诊断信息" }))
    expect(writeText).toHaveBeenCalledWith("错误码: AUTH_BACKEND_UNAVAILABLE\n请求标识: req_route")

    loaderShouldFail = false
    await userEvent.click(screen.getByRole("button", { name: "重试" }))

    expect(await screen.findByText("页面已恢复")).toBeInTheDocument()
    expect(loader).toHaveBeenCalledTimes(2)
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

  it("工作台入口明确当前建设状态且不伪造业务数据", () => {
    render(<FoundationPage />)
    expect(screen.getByRole("heading", {
      name: "把长周期判断，建立在可验证的数据上。",
    })).toBeInTheDocument()
    expect(screen.getByRole("region", { name: "当前建设状态" })).toHaveTextContent(
      "使用真实接口展示系统状态和监控股票，不使用演示数据。",
    )
  })
})
