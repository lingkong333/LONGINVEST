import { QueryClientProvider } from "@tanstack/react-query"
import { useState } from "react"
import { RouterProvider } from "react-router-dom"

import { createAppQueryClient } from "@/shared/query/query-client"

interface AppProvidersProps {
  router: React.ComponentProps<typeof RouterProvider>["router"]
}

export function AppProviders({ router }: AppProvidersProps) {
  const [queryClient] = useState(createAppQueryClient)

  return (
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} future={{ v7_startTransition: true }} />
    </QueryClientProvider>
  )
}
