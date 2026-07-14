import { AppErrorBoundary } from "@/app/app-error-boundary"
import { AppProviders } from "@/app/app-providers"
import { appRouter } from "@/app/router"

export function App() {
  return (
    <AppErrorBoundary>
      <AppProviders router={appRouter} />
    </AppErrorBoundary>
  )
}
