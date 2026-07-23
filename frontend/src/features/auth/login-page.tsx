import { ArrowRight, CandlestickChart } from "lucide-react"
import { useState } from "react"
import { Navigate, useLocation, useNavigate } from "react-router-dom"
import { z } from "zod"

import { useAuth } from "@/features/auth/auth-context"
import { AppearanceMenu } from "@/app/appearance-menu"
import { toErrorDiagnostic } from "@/shared/errors/error-diagnostic"
import { useZodForm } from "@/shared/forms/use-zod-form"
import { Alert, AlertDescription } from "@/shared/ui/alert"
import { Button } from "@/shared/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/shared/ui/card"
import { FormField } from "@/shared/ui/form-field"
import { Input } from "@/shared/ui/input"
import { PageState } from "@/shared/ui/page-state"

const loginSchema = z.object({
  username: z.string().trim().min(1, "必填").max(128),
  password: z.string().min(1, "必填").max(128),
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
      <main className="grid min-h-screen place-items-center p-4">
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
      <main className="grid min-h-screen place-items-center p-4">
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
    <main className="relative grid min-h-screen place-items-center p-4">
      <div className="absolute right-4 top-4">
        <AppearanceMenu />
      </div>
      <Card className="w-full max-w-sm" aria-labelledby="login-title">
        <CardHeader>
          <div className="mb-2 flex size-10 items-center justify-center rounded-md bg-primary text-primary-foreground">
            <CandlestickChart aria-hidden="true" />
          </div>
          <CardTitle>
            <h1 id="login-title">登录工作台</h1>
          </CardTitle>
          <CardDescription>LONGINVEST</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={submit} className="grid gap-4" noValidate>
            <FormField control={form.control} name="username" label="用户名">
              {({ field }) => (
                <Input
                  {...field}
                  autoComplete="username"
                  autoFocus
                  placeholder="用户名"
                />
              )}
            </FormField>
            <FormField control={form.control} name="password" label="密码">
              {({ field }) => (
                <Input
                  {...field}
                  type="password"
                  autoComplete="current-password"
                  placeholder="密码"
                />
              )}
            </FormField>

            {submitError ? (
              <Alert variant="destructive">
                <AlertDescription><code>{toErrorDiagnostic(submitError).code}</code></AlertDescription>
              </Alert>
            ) : null}

            <Button
              type="submit"
              size="lg"
              disabled={auth.isSubmitting}
              aria-label={auth.isSubmitting ? "正在登录" : "登录"}
            >
              <span>{auth.isSubmitting ? "登录中" : "登录"}</span>
              <ArrowRight aria-hidden="true" />
            </Button>
          </form>
        </CardContent>
      </Card>
    </main>
  )
}
