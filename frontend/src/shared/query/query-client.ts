import { QueryClient } from "@tanstack/react-query"

import { ApiError } from "@/shared/api/client"

function canRetryQuery(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status !== undefined) {
      return error.status >= 500 && error.status < 600
    }
    return error.code === "REQUEST_TIMEOUT" || error.code === "NETWORK_ERROR"
  }
  return error instanceof TypeError
}

export function createAppQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 10_000,
        gcTime: 5 * 60_000,
        retry: (failureCount, error) => failureCount < 1 && canRetryQuery(error),
        refetchOnWindowFocus: false,
      },
      mutations: {
        retry: false,
      },
    },
  })
}
