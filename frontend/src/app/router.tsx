import { createBrowserRouter } from "react-router-dom"

import { FoundationPage } from "@/pages/foundation-page"
import { PageState } from "@/shared/ui/page-state"

export const appRouter = createBrowserRouter([
  {
    path: "/",
    element: <FoundationPage />,
    errorElement: (
      <PageState
        state="error"
        title="页面无法打开"
        description="路由加载失败，请返回后重试。"
      />
    ),
  },
])
