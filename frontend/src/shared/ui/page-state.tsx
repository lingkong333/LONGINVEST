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
import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/shared/ui/empty"
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
    <Empty role={role} aria-label={title} aria-live="polite">
      <EmptyHeader>
        <EmptyMedia variant="icon" aria-hidden="true">
        <Icon className={isLoading ? "animate-spin" : undefined} />
        </EmptyMedia>
        <EmptyTitle>{title}</EmptyTitle>
        <EmptyDescription>{description}</EmptyDescription>
      </EmptyHeader>
      <EmptyContent>
        {isLoading ? (
          <div className="grid w-full max-w-sm gap-2" aria-hidden="true">
            <Skeleton className="h-2 w-full" />
            <Skeleton className="h-2 w-2/3" />
          </div>
        ) : null}
        {error ? (
          <dl className="grid w-full max-w-sm gap-2 rounded-md border bg-muted/40 p-3 text-sm">
            <div className="flex justify-between gap-4">
              <dt className="text-muted-foreground">错误码</dt>
              <dd className="font-mono">{error.code}</dd>
            </div>
            {error.requestId ? (
              <div className="flex justify-between gap-4">
                <dt className="text-muted-foreground">请求标识</dt>
                <dd className="break-all font-mono">{error.requestId}</dd>
              </div>
            ) : null}
          </dl>
        ) : null}
        {action || diagnostic ? (
          <div className="flex items-center justify-center gap-2">
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
      </EmptyContent>
    </Empty>
  )
}
