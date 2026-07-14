import {
  AlertTriangleIcon,
  Clock3Icon,
  CopyIcon,
  InboxIcon,
  LoaderCircleIcon,
  RefreshCwIcon,
  ServerCrashIcon,
  WifiOffIcon,
} from "lucide-react"

import { Button } from "@/shared/ui/button"
import { Skeleton } from "@/shared/ui/skeleton"

type PageStateKind =
  | "loading"
  | "empty"
  | "error"
  | "partial"
  | "stale"
  | "calculating"
  | "timeout"
  | "offline"
  | "unavailable"
  | "conflict"

interface PageStateProps {
  state: PageStateKind
  title: string
  description: string
  action?: {
    label: string
    onClick: () => void
  }
  error?: {
    code: string
    requestId?: string
  }
}

const stateIcon = {
  loading: LoaderCircleIcon,
  empty: InboxIcon,
  error: AlertTriangleIcon,
  partial: AlertTriangleIcon,
  stale: Clock3Icon,
  calculating: LoaderCircleIcon,
  timeout: Clock3Icon,
  offline: WifiOffIcon,
  unavailable: ServerCrashIcon,
  conflict: RefreshCwIcon,
} satisfies Record<PageStateKind, typeof AlertTriangleIcon>

export function PageState({ state, title, description, action, error }: PageStateProps) {
  const Icon = stateIcon[state]
  const isLoading = state === "loading" || state === "calculating"
  const role = state === "error" || state === "unavailable" ? "alert" : "status"
  const diagnostic = error
    ? [`错误码: ${error.code}`, error.requestId ? `请求标识: ${error.requestId}` : null]
        .filter(Boolean)
        .join("\n")
    : null

  return (
    <section className="page-state" role={role} aria-label={title} aria-live="polite">
      <div className="page-state__icon" aria-hidden="true">
        <Icon className={isLoading ? "animate-spin" : undefined} />
      </div>
      <div className="page-state__content">
        <h2>{title}</h2>
        <p>{description}</p>
        {isLoading ? (
          <div className="page-state__progress" aria-hidden="true">
            <Skeleton className="h-2 w-full" />
            <Skeleton className="h-2 w-2/3" />
          </div>
        ) : null}
        {error ? (
          <dl className="page-state__details">
            <div><dt>错误码</dt><dd>{error.code}</dd></div>
            {error.requestId ? <div><dt>请求标识</dt><dd>{error.requestId}</dd></div> : null}
          </dl>
        ) : null}
        {action || diagnostic ? (
          <div className="page-state__actions">
            {action ? <Button onClick={action.onClick}>{action.label}</Button> : null}
            {diagnostic ? (
              <Button
                type="button"
                variant="outline"
                size="icon"
                aria-label="复制诊断信息"
                title="复制诊断信息"
                onClick={() => void navigator.clipboard?.writeText(diagnostic)}
              >
                <CopyIcon data-icon="inline-start" />
              </Button>
            ) : null}
          </div>
        ) : null}
      </div>
    </section>
  )
}
