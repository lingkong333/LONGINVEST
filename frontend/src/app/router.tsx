import { createBrowserRouter } from "react-router-dom"

import { RouteErrorPage } from "@/app/route-error-page"
import { FoundationPage } from "@/pages/foundation-page"

export const appRouter = createBrowserRouter([
  {
    path: "/",
    element: <FoundationPage />,
    errorElement: <RouteErrorPage />,
  },
])
