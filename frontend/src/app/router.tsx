import { lazy, Suspense, type ReactNode } from "react"
import { createBrowserRouter, Navigate } from "react-router-dom"

import { AppShell } from "@/app/app-shell"
import { LegacyStockDetailRedirect } from "@/app/legacy-stock-detail-redirect"
import { RouteErrorPage } from "@/app/route-error-page"
import { LoginPage, ProtectedRoute } from "@/features/auth"
import { DashboardPage } from "@/features/dashboard"
import { MonitoringPage } from "@/features/monitoring"
import { PositionsPage } from "@/features/positions"
import { SignalsPage } from "@/features/signals"
import { TargetManagementPage } from "@/features/targets"
import { PageState } from "@/shared/ui/page-state"

const MarketDataPage = lazy(async () => {
  const module = await import("@/features/market-data/market-data-page")
  return { default: module.MarketDataPage }
})
const SecurityListPage = lazy(async () => {
  const module = await import("@/features/market-data/security-list-page")
  return { default: module.SecurityListPage }
})
const SecurityDetailPage = lazy(async () => {
  const module = await import("@/features/market-data/security-detail-page")
  return { default: module.SecurityDetailPage }
})
const HistoryBackfillPage = lazy(async () => {
  const module = await import("@/features/market-data/history-backfill-page")
  return { default: module.HistoryBackfillPage }
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
const StrategyScreeningPage = lazy(async () => {
  const module = await import("@/features/strategies")
  return { default: module.StrategyScreeningPage }
})
const CandidateBacktestsPage = lazy(async () => {
  const module = await import("@/features/strategies")
  return { default: module.CandidateBacktestsPage }
})
const CandidateBacktestDetailPage = lazy(async () => {
  const module = await import("@/features/strategies")
  return { default: module.CandidateBacktestDetailPage }
})
const CalendarPage = lazy(async () => {
  const module = await import("@/features/calendar")
  return { default: module.CalendarPage }
})
const SystemStatusPage = lazy(async () => {
  const module = await import("@/features/system-status")
  return { default: module.SystemStatusPage }
})
const AuditPage = lazy(async () => {
  const module = await import("@/features/audit")
  return { default: module.AuditPage }
})
const SettingsPage = lazy(async () => {
  const module = await import("@/features/settings")
  return { default: module.SettingsPage }
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
            path: "/screenings",
            element: deferredPage(<StrategyScreeningPage />),
          },
          {
            path: "/backtests",
            element: deferredPage(<CandidateBacktestsPage />),
          },
          {
            path: "/backtests/:taskId",
            element: deferredPage(<CandidateBacktestsPage />),
          },
          {
            path: "/backtests/:taskId/items/:itemId",
            element: deferredPage(<CandidateBacktestDetailPage />),
          },
          {
            path: "/market-data",
            element: deferredPage(<MarketDataPage />),
          },
          {
            path: "/stocks",
            element: deferredPage(<SecurityListPage />),
          },
          {
            path: "/stocks/:symbol",
            element: deferredPage(<SecurityDetailPage />),
          },
          {
            path: "/market-data/backfills/:jobId",
            element: deferredPage(<HistoryBackfillPage />),
          },
          {
            path: "/market-data/stocks",
            element: <Navigate to="/stocks" replace />,
          },
          {
            path: "/market-data/stocks/:symbol",
            element: <LegacyStockDetailRedirect />,
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
          {
            path: "/system-status",
            element: deferredPage(<SystemStatusPage />),
          },
          {
            path: "/audit",
            element: deferredPage(<AuditPage />),
          },
          {
            path: "/settings",
            element: deferredPage(<SettingsPage />),
          },
        ],
      },
    ],
  },
])
