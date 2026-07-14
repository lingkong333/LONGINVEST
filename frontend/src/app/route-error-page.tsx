import { useMemo } from "react"
import { useRouteError } from "react-router-dom"

import { toErrorDiagnostic } from "@/shared/errors/error-diagnostic"
import { PageState } from "@/shared/ui/page-state"

export function RouteErrorPage() {
  const routeError = useRouteError()
  const diagnostic = useMemo(() => toErrorDiagnostic(routeError), [routeError])

  return (
    <PageState
      state="error"
      title="页面无法打开"
      description="路由加载失败，请返回后重试。"
      error={diagnostic}
    />
  )
}
