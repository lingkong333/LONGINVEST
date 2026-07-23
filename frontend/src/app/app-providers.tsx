import { QueryClientProvider } from "@tanstack/react-query"
import { useState } from "react"
import { RouterProvider } from "react-router-dom"

import { AppearanceProvider } from "@/app/appearance-provider"
import { AuthProvider } from "@/features/auth"
import { createAppQueryClient } from "@/shared/query/query-client"
import { Toaster } from "@/shared/ui/sonner"

interface AppProvidersProps {
  router: React.ComponentProps<typeof RouterProvider>["router"]
}

export function AppProviders({ router }: AppProvidersProps) {
  const [queryClient] = useState(createAppQueryClient)

  return (
    <AppearanceProvider>
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <RouterProvider router={router} future={{ v7_startTransition: true }} />
          <Toaster richColors closeButton />
        </AuthProvider>
      </QueryClientProvider>
    </AppearanceProvider>
  )
}
