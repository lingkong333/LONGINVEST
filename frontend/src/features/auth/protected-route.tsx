import { Navigate, Outlet, useLocation } from "react-router-dom"

import { useAuth } from "@/features/auth/auth-context"
import { toErrorDiagnostic } from "@/shared/errors/error-diagnostic"
import { PageState } from "@/shared/ui/page-state"

export function ProtectedRoute() {
  const auth = useAuth()
  const location = useLocation()

  if (auth.phase === "bootstrapping") {
    return (
      <main className="auth-state-page">
        <PageState
          state="loading"
          title="正在确认登录状态"
          description="正在安全地恢复你的会话。"
        />
      </main>
    )
  }

  if (auth.phase === "unavailable") {
    return (
      <main className="auth-state-page">
        <PageState
          state="error"
          title="认证服务暂不可用"
          description="系统没有把这次故障误判为退出登录。请稍后重试。"
          error={toErrorDiagnostic(auth.error)}
          action={{ label: "重新连接", onClick: () => void auth.retry() }}
        />
      </main>
    )
  }

  if (auth.phase === "unauthenticated") {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }

  return <Outlet />
}
