import { Component, type ErrorInfo, type ReactNode } from "react"

import { PageState } from "@/shared/ui/page-state"

interface AppErrorBoundaryProps {
  children: ReactNode
  onError?: (error: Error, info: ErrorInfo) => void
  onReset?: () => void
}

interface AppErrorBoundaryState {
  hasError: boolean
}

export class AppErrorBoundary extends Component<
  AppErrorBoundaryProps,
  AppErrorBoundaryState
> {
  state: AppErrorBoundaryState = { hasError: false }

  static getDerivedStateFromError(): AppErrorBoundaryState {
    return { hasError: true }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    this.props.onError?.(error, info)
  }

  private reset = () => {
    this.props.onReset?.()
    this.setState({ hasError: false })
  }

  render() {
    if (this.state.hasError) {
      return (
        <PageState
          state="error"
          title="页面出现异常"
          description="当前区域未能正常显示，其他功能不受影响。"
          action={{ label: "重新尝试", onClick: this.reset }}
        />
      )
    }

    return this.props.children
  }
}
