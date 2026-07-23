import { createContext, useContext } from "react"

import type { AuthState, LoginInput } from "@/features/auth/types"

export type AuthPhase =
  | "bootstrapping"
  | "authenticated"
  | "unauthenticated"
  | "unavailable"

export interface AuthContextValue {
  phase: AuthPhase
  auth: AuthState | null
  error: unknown
  isSubmitting: boolean
  invalidate(): void
  login(input: LoginInput): Promise<void>
  logout(): Promise<void>
  retry(): Promise<void>
}

export const AuthContext = createContext<AuthContextValue | null>(null)

export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) {
    throw new Error("useAuth 必须在 AuthProvider 内使用")
  }
  return context
}
