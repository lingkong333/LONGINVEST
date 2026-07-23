import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import { JobsPage } from "@/features/jobs/jobs-page"
import type {
  JobDetails,
  JobGateway,
  JobSummary,
} from "@/features/jobs/types"

const job: JobSummary = {
  id: "00000000-0000-4000-8000-000000000001",
  jobType: "BULK_HISTORY",
  businessObjectType: "history_backfill",
  businessObjectId: "backfill-1",
  queue: "bulk-history",
  priority: 50,
  status: "RUNNING",
  progress: { completed: 3, total: 10 },
  resultSummary: null,
  currentRunId: "run-1",
  version: 4,
  createdAt: "2026-07-23T03:00:00Z",
  updatedAt: "2026-07-23T04:00:00Z",
  terminalAt: null,
}

const details: JobDetails = {
  job: {
    ...job,
    configSnapshot: {},
    requestId: "req-create",
    createdByUserId: "user-1",
    softTimeoutSeconds: 300,
    hardTimeoutSeconds: 600,
  },
  runs: [{
    id: "run-1",
    jobId: job.id,
    attemptNo: 1,
    workerId: "worker-1",
    status: "RUNNING",
    claimedAt: "2026-07-23T03:01:00Z",
    startedAt: "2026-07-23T03:01:01Z",
    endedAt: null,
    heartbeatAt: "2026-07-23T04:00:00Z",
    exitType: null,
    errorCode: null,
    errorSummary: null,
    metrics: null,
  }],
  items: [{
    id: "item-1",
    jobId: job.id,
    itemKey: "600000.SH",
    status: "FAILED",
    attemptCount: 1,
    resultRef: null,
    errorCode: "PROVIDER_TIMEOUT",
    createdAt: "2026-07-23T03:00:00Z",
    startedAt: "2026-07-23T03:02:00Z",
    endedAt: "2026-07-23T03:03:00Z",
    updatedAt: "2026-07-23T03:03:00Z",
  }],
  itemPagination: { page: 1, pageSize: 100, total: 1 },
  allowedActions: ["pause"],
}

function gateway(overrides: Partial<JobGateway> = {}): JobGateway {
  return {
    loadJobs: vi.fn().mockResolvedValue({
      items: [job],
      pagination: { page: 1, pageSize: 20, total: 1 },
    }),
    loadDetails: vi.fn().mockResolvedValue(details),
    runAction: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  }
}

function renderPage(api: JobGateway) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
  render(
    <QueryClientProvider client={client}>
      <JobsPage gateway={api} />
    </QueryClientProvider>,
  )
}

describe("任务管理页面", () => {
  it("列表等待响应时显示加载状态", () => {
    renderPage(gateway({
      loadJobs: vi.fn().mockReturnValue(new Promise(() => undefined)),
    }))

    expect(screen.getByText("正在读取任务")).toBeInTheDocument()
  })

  it("加载完成后展示任务、运行尝试和逐项结果", async () => {
    renderPage(gateway())

    expect(await screen.findByText("BULK_HISTORY")).toBeInTheDocument()
    expect(screen.getByText("3 / 10")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "查看详情" }))

    expect(await screen.findByText("worker-1")).toBeInTheDocument()
    expect(screen.getByText("600000.SH")).toBeInTheDocument()
    expect(screen.getByText("PROVIDER_TIMEOUT")).toBeInTheDocument()
    expect(screen.getByText(/不提供命令执行或原始日志/)).toBeInTheDocument()
  })

  it("没有任务时显示明确空状态", async () => {
    renderPage(gateway({
      loadJobs: vi.fn().mockResolvedValue({
        items: [],
        pagination: { page: 1, pageSize: 20, total: 0 },
      }),
    }))

    expect(await screen.findByText("没有符合条件的任务")).toBeInTheDocument()
  })

  it("列表失败时提供诊断和重试入口", async () => {
    renderPage(gateway({
      loadJobs: vi.fn().mockRejectedValue(new Error("list failed")),
    }))

    expect(await screen.findByText("任务列表暂时无法读取")).toBeInTheDocument()
    expect(screen.getByText("UNKNOWN_ERROR")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "重新加载" }))
      .toBeInTheDocument()
  })

  it("筛选条件提交到网关并从第一页重新读取", async () => {
    const api = gateway()
    const user = userEvent.setup()
    renderPage(api)
    await screen.findByText("BULK_HISTORY")

    await user.type(screen.getByLabelText("任务类型"), "QFQ_REFRESH")
    await user.type(screen.getByLabelText("队列"), "qfq-refresh")
    await user.click(screen.getByLabelText("任务状态"))
    await user.click(screen.getByRole("option", { name: "失败" }))
    await user.click(screen.getByRole("button", { name: "查询" }))

    expect(api.loadJobs).toHaveBeenLastCalledWith({
      page: 1,
      pageSize: 20,
      status: "FAILED",
      jobType: "QFQ_REFRESH",
      queue: "qfq-refresh",
    })
  })

  it("后端未允许的操作保持禁用", async () => {
    const api = gateway()
    renderPage(api)
    await userEvent.click(await screen.findByRole("button", {
      name: "查看详情",
    }))

    expect(await screen.findByRole("button", { name: "暂停" }))
      .toBeEnabled()
    expect(screen.getByRole("button", { name: "取消" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "继续" })).toBeDisabled()
    expect(api.runAction).not.toHaveBeenCalled()
  })

  it("操作要求原因、阻止重复提交并在成功后刷新", async () => {
    let finish: (() => void) | undefined
    const runAction = vi.fn().mockImplementation(
      () => new Promise<void>((resolve) => { finish = resolve }),
    )
    const api = gateway({ runAction })
    renderPage(api)
    await userEvent.click(await screen.findByRole("button", {
      name: "查看详情",
    }))
    await userEvent.click(await screen.findByRole("button", { name: "暂停" }))

    const dialogs = screen.getAllByRole("dialog")
    const confirmation = dialogs[dialogs.length - 1]
    const submit = within(confirmation).getByRole("button", {
      name: "确认执行",
    })
    expect(submit).toBeDisabled()
    await userEvent.type(
      within(confirmation).getByLabelText("操作原因"),
      "释放实时任务资源",
    )
    await userEvent.click(submit)
    await userEvent.click(submit)

    expect(runAction).toHaveBeenCalledTimes(1)
    expect(runAction).toHaveBeenCalledWith({
      jobId: job.id,
      action: "pause",
      reason: "释放实时任务资源",
      expectedVersion: 4,
    })
    expect(submit).toBeDisabled()
    finish?.()
    expect(await screen.findByText("暂停请求已受理。")).toBeInTheDocument()
    expect(api.loadJobs).toHaveBeenCalledTimes(2)
    expect(api.loadDetails).toHaveBeenCalledTimes(2)
  })

  it("操作失败时保留原因并显示错误", async () => {
    const api = gateway({
      runAction: vi.fn().mockRejectedValue(new Error("任务版本已变化")),
    })
    renderPage(api)
    await userEvent.click(await screen.findByRole("button", {
      name: "查看详情",
    }))
    await userEvent.click(await screen.findByRole("button", { name: "暂停" }))

    const dialogs = screen.getAllByRole("dialog")
    const confirmation = dialogs[dialogs.length - 1]
    const reason = within(confirmation).getByLabelText("操作原因")
    await userEvent.type(reason, "释放实时任务资源")
    await userEvent.click(within(confirmation).getByRole("button", {
      name: "确认执行",
    }))

    expect(await within(confirmation).findByText("任务版本已变化"))
      .toBeInTheDocument()
    expect(reason).toHaveValue("释放实时任务资源")
  })
})
