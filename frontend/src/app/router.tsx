import { lazy, Suspense, type ReactNode } from "react"
import { createBrowserRouter } from "react-router-dom"

import { AppShell } from "@/app/app-shell"
import { RouteErrorPage } from "@/app/route-error-page"
import { LoginPage, ProtectedRoute } from "@/features/auth"
import { DashboardPage } from "@/features/dashboard"
import { MonitoringPage } from "@/features/monitoring"
import { PositionsPage } from "@/features/positions"
import { SignalsPage } from "@/features/signals"
import { TargetManagementPage } from "@/features/targets"
import { PageState } from "@/shared/ui/page-state"

const MarketDataPage = lazy(async () => {
  const module = await import("@/features/market-data")
  return { default: module.MarketDataPage }
})
const NotificationsPage = lazy(async () => {
  const module = await import("@/features/notifications")
  return { default: module.NotificationsPage }
})
const StrategyOperationsPage = lazy(async () => {
  const module = await import("@/features/strategies")
  return { default: module.StrategyOperationsPage }
})
const JobsPage = lazy(async () => {
  const module = await import("@/features/jobs")
  return { default: module.JobsPage }
})
const ProvidersPage = lazy(async () => {
  const module = await import("@/features/providers")
  return { default: module.ProvidersPage }
})
const AlertsPage = lazy(async () => {
  const module = await import("@/features/alerts")
  return { default: module.AlertsPage }
})
const CalendarPage = lazy(async () => {
  const module = await import("@/features/calendar")
  return { default: module.CalendarPage }
})

function deferredPage(element: ReactNode) {
  return (
    <Suspense
      fallback={<PageState state="loading" title="正在加载页面" description="正在准备当前工作区。" />}
    >
      {element}
    </Suspense>
  )
}

export const appRouter = createBrowserRouter([
  {
    path: "/login",
    element: <LoginPage />,
    errorElement: <RouteErrorPage />,
  },
  {
    element: <ProtectedRoute />,
    errorElement: <RouteErrorPage />,
    children: [
      {
        element: <AppShell />,
        children: [
          {
            path: "/",
            element: <DashboardPage />,
          },
          {
            path: "/monitoring",
            element: <MonitoringPage />,
          },
          {
            path: "/positions",
            element: <PositionsPage />,
          },
          {
            path: "/targets",
            element: <TargetManagementPage />,
          },
          {
            path: "/signals",
            element: <SignalsPage />,
          },
          {
            path: "/strategies",
            element: deferredPage(<StrategyOperationsPage />),
          },
          {
            path: "/backtests",
            element: deferredPage(<StrategyOperationsPage initialView="backtest" />),
          },
          {
            path: "/market-data",
            element: deferredPage(<MarketDataPage />),
          },
          {
            path: "/notifications",
            element: deferredPage(<NotificationsPage />),
          },
          {
            path: "/jobs",
            element: deferredPage(<JobsPage />),
          },
          {
            path: "/providers",
            element: deferredPage(<ProvidersPage />),
          },
          {
            path: "/alerts",
            element: deferredPage(<AlertsPage />),
          },
          {
            path: "/calendar",
            element: deferredPage(<CalendarPage />),
          },
        ],
      },
    ],
  },
])
