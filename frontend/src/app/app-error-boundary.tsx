import { Component, type ErrorInfo, type ReactNode } from "react"

import {
  toErrorDiagnostic,
  type ErrorDiagnostic,
} from "@/shared/errors/error-diagnostic"
import { PageState } from "@/shared/ui/page-state"

interface AppErrorBoundaryProps {
  children: ReactNode
  onError?: (error: Error, info: ErrorInfo) => void
  onReset?: () => void
}

interface AppErrorBoundaryState {
  hasError: boolean
  diagnostic?: ErrorDiagnostic
}

export class AppErrorBoundary extends Component<
  AppErrorBoundaryProps,
  AppErrorBoundaryState
> {
  state: AppErrorBoundaryState = { hasError: false }

  static getDerivedStateFromError(error: unknown): AppErrorBoundaryState {
    return { hasError: true, diagnostic: toErrorDiagnostic(error) }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    this.props.onError?.(error, info)
  }

  private reset = () => {
    this.props.onReset?.()
    this.setState({ hasError: false, diagnostic: undefined })
  }

  render() {
    if (this.state.hasError) {
      return (
        <PageState
          state="error"
          title="页面出现异常"
          description="当前区域未能正常显示，其他功能不受影响。"
          error={this.state.diagnostic}
          action={{ label: "重新尝试", onClick: this.reset }}
        />
      )
    }

    return this.props.children
  }
}
