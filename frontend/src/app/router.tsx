import { createBrowserRouter } from "react-router-dom"

import { AppShell } from "@/app/app-shell"
import { RouteErrorPage } from "@/app/route-error-page"
import { LoginPage, ProtectedRoute } from "@/features/auth"
import { DashboardPage } from "@/features/dashboard"
import { MonitoringPage } from "@/features/monitoring"

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
        ],
      },
    ],
  },
])
