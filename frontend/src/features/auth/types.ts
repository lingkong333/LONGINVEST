export interface AuthUser {
  id: string
  username: string
  status: "ACTIVE" | "DISABLED"
}

export interface AuthSession {
  id: string
  status: string
  current: boolean
  created_at: string
  last_request_at: string
  last_user_activity_at: string
  absolute_expires_at: string
  ip_summary: string | null
  user_agent_summary: string | null
}

export interface AuthState {
  user: AuthUser
  session: AuthSession
}

export interface LoginInput {
  username: string
  password: string
}

export interface AuthGateway {
  loadSession(): Promise<AuthState>
  login(input: LoginInput): Promise<AuthState>
  logout(): Promise<void>
  setUnauthorizedHandler(handler: (() => void) | undefined): void
}
