import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { createMemoryRouter, RouterProvider } from "react-router-dom"
import { describe, expect, it, vi } from "vitest"

import { AppShell } from "@/app/app-shell"
import {
  AuthProvider,
  LoginPage,
  ProtectedRoute,
  type AuthGateway,
  type AuthState,
} from "@/features/auth"
import { ApiError } from "@/shared/api/client"

const authenticated: AuthState = {
  user: {
    id: "user-1",
    username: "admin",
    status: "ACTIVE",
  },
  session: {
    id: "session-1",
    status: "ACTIVE",
    current: true,
    created_at: "2026-07-23T00:00:00Z",
    last_request_at: "2026-07-23T00:00:00Z",
    last_user_activity_at: "2026-07-23T00:00:00Z",
    absolute_expires_at: "2026-10-23T00:00:00Z",
    ip_summary: "127.0.0.x",
    user_agent_summary: "test",
  },
}

function createGateway(
  loadSession: AuthGateway["loadSession"],
  login: AuthGateway["login"] = vi.fn().mockResolvedValue(authenticated),
) {
  let unauthorizedHandler: (() => void) | undefined
  const gateway: AuthGateway = {
    loadSession,
    login,
    logout: vi.fn().mockResolvedValue(undefined),
    setUnauthorizedHandler(handler) {
      unauthorizedHandler = handler
    },
  }
  return {
    gateway,
    expireSession() {
      unauthorizedHandler?.()
    },
  }
}

function renderApp(gateway: AuthGateway, initialPath = "/") {
  const router = createMemoryRouter([
    {
      path: "/login",
      element: <LoginPage />,
    },
    {
      element: <ProtectedRoute />,
      children: [
        {
          element: <AppShell />,
          children: [{ path: "/", element: <p>Workspace ready</p> }],
        },
      ],
    },
  ], { initialEntries: [initialPath] })
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })

  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider gateway={gateway}>
        <RouterProvider router={router} future={{ v7_startTransition: true }} />
      </AuthProvider>
    </QueryClientProvider>,
  )
}

describe("登录和会话启动", () => {
  it("有效会话直接进入工作台，并可退出", async () => {
    const { gateway } = createGateway(vi.fn().mockResolvedValue(authenticated))
    renderApp(gateway)

    expect(await screen.findByText("Workspace ready")).toBeInTheDocument()

    await userEvent.click(screen.getByRole("button", { name: "退出登录" }))

    expect(gateway.logout).toHaveBeenCalledOnce()
    expect(await screen.findByRole("heading", { name: "登录工作台" })).toBeInTheDocument()
  })

  it("未登录时进入登录页，成功后回到原受保护页面", async () => {
    const login = vi.fn().mockResolvedValue(authenticated)
    const { gateway } = createGateway(
      vi.fn().mockRejectedValue(new ApiError("请登录", {
        code: "AUTH_REQUIRED",
        status: 401,
      })),
      login,
    )
    renderApp(gateway)

    expect(await screen.findByRole("heading", { name: "登录工作台" })).toBeInTheDocument()
    await userEvent.type(screen.getByRole("textbox", { name: "Username" }), "admin")
    await userEvent.type(screen.getByLabelText("Password"), "correct-password")
    await userEvent.click(screen.getByRole("button", { name: "登录" }))

    expect(login).toHaveBeenCalledWith({
      username: "admin",
      password: "correct-password",
    })
    expect(await screen.findByText("Workspace ready")).toBeInTheDocument()
  })

  it("认证服务不可用时显示故障，不误判成退出登录", async () => {
    const { gateway } = createGateway(
      vi.fn().mockRejectedValue(new ApiError("认证服务不可用", {
        code: "AUTH_BACKEND_UNAVAILABLE",
        requestId: "req-auth-down",
        status: 503,
      })),
    )
    renderApp(gateway)

    expect(await screen.findByRole("heading", {
      name: "认证服务暂不可用",
    })).toBeInTheDocument()
    expect(screen.getByText("AUTH_BACKEND_UNAVAILABLE")).toBeInTheDocument()
    expect(screen.queryByRole("heading", { name: "登录工作台" })).not.toBeInTheDocument()
  })

  it("会话在使用中失效时统一返回登录页", async () => {
    const { gateway, expireSession } = createGateway(
      vi.fn().mockResolvedValue(authenticated),
    )
    renderApp(gateway)

    expect(await screen.findByText("Workspace ready")).toBeInTheDocument()
    expireSession()

    expect(await screen.findByRole("heading", { name: "登录工作台" })).toBeInTheDocument()
  })
})
