import { ArrowRight, CandlestickChart } from "lucide-react"
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
  username: z.string().trim().min(1, "REQUIRED").max(128),
  password: z.string().min(1, "REQUIRED").max(128),
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
      <section className="login-visual" aria-hidden="true">
        <div className="login-visual__orbit">
          <CandlestickChart />
          <i />
          <i />
          <i />
        </div>
        <div className="login-visual__bars">
          {Array.from({ length: 18 }, (_, index) => <i key={index} />)}
        </div>
      </section>

      <section className="login-panel" aria-labelledby="login-title">
        <div className="login-panel__inner">
          <div className="login-brand">
            <span className="login-brand__mark"><CandlestickChart aria-hidden="true" /></span>
            <span>LONGINVEST</span>
          </div>
          <h1 id="login-title" className="sr-only">登录工作台</h1>

          <form onSubmit={submit} className="login-form" noValidate>
            <FormField control={form.control} name="username" label="Username">
              {({ field }) => (
                <Input
                  {...field}
                  autoComplete="username"
                  autoFocus
                  placeholder="Username"
                />
              )}
            </FormField>
            <FormField control={form.control} name="password" label="Password">
              {({ field }) => (
                <Input
                  {...field}
                  type="password"
                  autoComplete="current-password"
                  placeholder="Password"
                />
              )}
            </FormField>

            {submitError ? (
              <div className="login-error" role="alert">
                <code>{toErrorDiagnostic(submitError).code}</code>
              </div>
            ) : null}

            <Button
              type="submit"
              size="lg"
              disabled={auth.isSubmitting}
              aria-label={auth.isSubmitting ? "正在登录" : "登录"}
            >
              <span>{auth.isSubmitting ? "CONNECTING" : "ENTER"}</span>
              <ArrowRight aria-hidden="true" />
            </Button>
          </form>
        </div>
      </section>
    </main>
  )
}
