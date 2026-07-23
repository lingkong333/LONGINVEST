import { ArrowRight, CandlestickChart, ShieldCheck } from "lucide-react"
import { useState } from "react"
import { Navigate, useLocation, useNavigate } from "react-router-dom"
import { z } from "zod"

import { useAuth } from "@/features/auth/auth-context"
import { toErrorDiagnostic } from "@/shared/errors/error-diagnostic"
import { useZodForm } from "@/shared/forms/use-zod-form"
import { Button } from "@/shared/ui/button"
import { FormField } from "@/shared/ui/form-field"
import { Input } from "@/shared/ui/input"
import { PageState } from "@/shared/ui/page-state"

const loginSchema = z.object({
  username: z.string().trim().min(1, "请输入用户名").max(128),
  password: z.string().min(1, "请输入密码").max(128),
})

export function LoginPage() {
  const auth = useAuth()
  const location = useLocation()
  const navigate = useNavigate()
  const [submitError, setSubmitError] = useState<unknown>()
  const form = useZodForm(loginSchema, {
    defaultValues: { username: "", password: "" },
  })

  if (auth.phase === "authenticated") {
    return <Navigate to="/" replace />
  }

  if (auth.phase === "bootstrapping") {
    return (
      <main className="auth-state-page">
        <PageState
          state="loading"
          title="正在确认登录状态"
          description="已登录时将直接进入工作台。"
        />
      </main>
    )
  }

  if (auth.phase === "unavailable") {
    return (
      <main className="auth-state-page">
        <PageState
          state="error"
          title="认证服务暂不可用"
          description="这不是密码错误，也不会清除现有登录状态。"
          error={toErrorDiagnostic(auth.error)}
          action={{ label: "重新连接", onClick: () => void auth.retry() }}
        />
      </main>
    )
  }

  const destination = typeof location.state === "object"
    && location.state
    && "from" in location.state
    && typeof location.state.from === "string"
    ? location.state.from
    : "/"

  const submit = form.handleSubmit(async (input) => {
    setSubmitError(undefined)
    try {
      await auth.login(input)
      navigate(destination, { replace: true })
    } catch (error) {
      setSubmitError(error)
    }
  })

  return (
    <main className="login-page">
      <section className="login-page__story" aria-label="LongInvest 简介">
        <div className="login-brand">
          <span className="login-brand__mark"><CandlestickChart aria-hidden="true" /></span>
          <span>LONGINVEST</span>
        </div>
        <div className="login-page__headline">
          <p className="eyebrow">A 股长波段决策工作台</p>
          <h1>让价格区间，成为可复核的行动依据。</h1>
          <p>
            将行情、目标、信号、策略与回测放进同一条证据链，
            每一次变化都有来源，每一个动作都能追溯。
          </p>
        </div>
        <div className="login-page__trust">
          <ShieldCheck aria-hidden="true" />
          <span>会话与安全凭据仅保存在受保护的服务端 Cookie 和内存中</span>
        </div>
      </section>

      <section className="login-panel" aria-labelledby="login-title">
        <div className="login-panel__inner">
          <p className="eyebrow">安全入口</p>
          <h2 id="login-title">登录工作台</h2>
          <p className="login-panel__intro">使用服务器管理员创建的账户继续。</p>

          <form onSubmit={submit} className="login-form" noValidate>
            <FormField control={form.control} name="username" label="用户名">
              {({ field }) => (
                <Input
                  {...field}
                  autoComplete="username"
                  autoFocus
                  placeholder="请输入用户名"
                />
              )}
            </FormField>
            <FormField control={form.control} name="password" label="密码">
              {({ field }) => (
                <Input
                  {...field}
                  type="password"
                  autoComplete="current-password"
                  placeholder="请输入密码"
                />
              )}
            </FormField>

            {submitError ? (
              <div className="login-error" role="alert">
                <strong>登录未成功</strong>
                <span>请检查用户名和密码，或稍后重试。</span>
                <code>{toErrorDiagnostic(submitError).code}</code>
              </div>
            ) : null}

            <Button type="submit" size="lg" disabled={auth.isSubmitting}>
              {auth.isSubmitting ? "正在登录…" : "进入工作台"}
              <ArrowRight aria-hidden="true" />
            </Button>
          </form>
        </div>
      </section>
    </main>
  )
}
