import {
  Activity,
  BellRing,
  CalendarDays,
  CandlestickChart,
  ChartNoAxesCombined,
  ChevronRight,
  CircleGauge,
  FlaskConical,
  LogOut,
  Radar,
  Settings2,
  ShieldAlert,
} from "lucide-react"
import { NavLink, Outlet } from "react-router-dom"

import { useAuth } from "@/features/auth"
import { Button } from "@/shared/ui/button"

const futureNavigation = [
  { label: "监控", icon: Radar },
  { label: "策略", icon: FlaskConical },
  { label: "回测", icon: ChartNoAxesCombined },
  { label: "通知", icon: BellRing },
  { label: "任务", icon: Activity },
  { label: "告警", icon: ShieldAlert },
  { label: "日历", icon: CalendarDays },
  { label: "设置", icon: Settings2 },
]

export function AppShell() {
  const auth = useAuth()

  return (
    <div className="workspace-shell">
      <aside className="workspace-sidebar">
        <div className="workspace-brand">
          <span className="workspace-brand__mark">
            <CandlestickChart aria-hidden="true" />
          </span>
          <span>
            <strong>LongInvest</strong>
            <small>长波段工作台</small>
          </span>
        </div>

        <nav className="workspace-nav" aria-label="主导航">
          <p>工作区</p>
          <NavLink to="/" end>
            <CircleGauge aria-hidden="true" />
            <span>仪表盘</span>
            <ChevronRight className="workspace-nav__arrow" aria-hidden="true" />
          </NavLink>
          {futureNavigation.map(({ label, icon: Icon }) => (
            <span className="workspace-nav__future" key={label} aria-disabled="true">
              <Icon aria-hidden="true" />
              <span>{label}</span>
              <small>待接入</small>
            </span>
          ))}
        </nav>

        <div className="workspace-user">
          <div className="workspace-user__avatar" aria-hidden="true">
            {auth.auth?.user.username.slice(0, 1).toUpperCase()}
          </div>
          <div>
            <strong>{auth.auth?.user.username}</strong>
            <span>安全会话已连接</span>
          </div>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="退出登录"
            onClick={() => void auth.logout()}
            disabled={auth.isSubmitting}
          >
            <LogOut aria-hidden="true" />
          </Button>
        </div>
      </aside>

      <div className="workspace-main">
        <header className="workspace-topbar">
          <div>
            <span className="status-indicator" aria-hidden="true" />
            <span>系统连接正常</span>
          </div>
          <span className="workspace-topbar__market">CN · A 股 · Asia/Shanghai</span>
        </header>
        <Outlet />
      </div>
    </div>
  )
}
