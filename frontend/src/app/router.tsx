import { createBrowserRouter } from "react-router-dom"

import { AppShell } from "@/app/app-shell"
import { RouteErrorPage } from "@/app/route-error-page"
import { LoginPage, ProtectedRoute } from "@/features/auth"
import { DashboardPage } from "@/features/dashboard"
import { MonitoringPage } from "@/features/monitoring"
import { PositionsPage } from "@/features/positions"
import { SignalsPage } from "@/features/signals"
import { TargetManagementPage } from "@/features/targets"

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
        ],
      },
    ],
  },
])
