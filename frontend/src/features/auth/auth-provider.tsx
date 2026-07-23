import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react"

import { ApiError } from "@/shared/api/client"
import {
  AuthContext,
  type AuthContextValue,
  type AuthPhase,
} from "@/features/auth/auth-context"
import { authGateway } from "@/features/auth/gateway"
import type {
  AuthGateway,
  LoginInput,
} from "@/features/auth/types"

const AUTH_QUERY_KEY = ["auth", "session"] as const

function isUnauthenticated(error: unknown) {
  return error instanceof ApiError && error.status === 401
}

export function AuthProvider({
  children,
  gateway = authGateway,
}: {
  children: ReactNode
  gateway?: AuthGateway
}) {
  const queryClient = useQueryClient()
  const [sessionInvalidated, setSessionInvalidated] = useState(false)
  const authQuery = useQuery({
    queryKey: AUTH_QUERY_KEY,
    queryFn: gateway.loadSession,
    retry: false,
    enabled: !sessionInvalidated,
  })

  const invalidate = useCallback(() => {
    setSessionInvalidated(true)
    queryClient.removeQueries({ queryKey: AUTH_QUERY_KEY })
  }, [queryClient])

  useEffect(() => {
    gateway.setUnauthorizedHandler(invalidate)
    return () => gateway.setUnauthorizedHandler(undefined)
  }, [gateway, invalidate])

  const loginMutation = useMutation({
    mutationFn: (input: LoginInput) => gateway.login(input),
    onSuccess: (auth) => {
      setSessionInvalidated(false)
      queryClient.setQueryData(AUTH_QUERY_KEY, auth)
    },
  })

  const logoutMutation = useMutation({
    mutationFn: () => gateway.logout(),
    onSuccess: () => {
      setSessionInvalidated(true)
      queryClient.clear()
    },
  })

  const login = useCallback(async (input: LoginInput) => {
    await loginMutation.mutateAsync(input)
  }, [loginMutation])

  const logout = useCallback(async () => {
    await logoutMutation.mutateAsync()
  }, [logoutMutation])

  const retry = useCallback(async () => {
    setSessionInvalidated(false)
    await authQuery.refetch()
  }, [authQuery])

  const phase: AuthPhase = useMemo(() => {
    if (sessionInvalidated || isUnauthenticated(authQuery.error)) {
      return "unauthenticated"
    }
    if (authQuery.isPending) {
      return "bootstrapping"
    }
    if (authQuery.isError) {
      return "unavailable"
    }
    return "authenticated"
  }, [
    authQuery.error,
    authQuery.isError,
    authQuery.isPending,
    sessionInvalidated,
  ])

  const value = useMemo<AuthContextValue>(() => ({
    phase,
    auth: phase === "authenticated" ? (authQuery.data ?? null) : null,
    error: authQuery.error,
    isSubmitting: loginMutation.isPending || logoutMutation.isPending,
    invalidate,
    login,
    logout,
    retry,
  }), [
    authQuery.data,
    authQuery.error,
    invalidate,
    login,
    loginMutation.isPending,
    logout,
    logoutMutation.isPending,
    phase,
    retry,
  ])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
