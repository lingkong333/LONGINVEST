import {
  Activity,
  BellRing,
  CalendarDays,
  CandlestickChart,
  ChartNoAxesCombined,
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
  { label: "监控", title: "Monitor", icon: Radar },
  { label: "策略", title: "Strategy", icon: FlaskConical },
  { label: "回测", title: "Backtest", icon: ChartNoAxesCombined },
  { label: "通知", title: "Notifications", icon: BellRing },
  { label: "任务", title: "Jobs", icon: Activity },
  { label: "告警", title: "Alerts", icon: ShieldAlert },
  { label: "日历", title: "Calendar", icon: CalendarDays },
  { label: "设置", title: "Settings", icon: Settings2 },
]

export function AppShell() {
  const auth = useAuth()

  return (
    <div className="workspace-shell">
      <aside className="workspace-sidebar">
        <span className="workspace-brand__mark" title="LongInvest">
          <CandlestickChart aria-hidden="true" />
        </span>

        <nav className="workspace-nav" aria-label="主导航">
          <NavLink to="/" end aria-label="仪表盘" title="Dashboard">
            <CircleGauge aria-hidden="true" />
          </NavLink>
          {futureNavigation.map(({ label, title, icon: Icon }) => (
            <span
              className="workspace-nav__future"
              key={label}
              aria-disabled="true"
              aria-label={label}
              title={title}
            >
              <Icon aria-hidden="true" />
            </span>
          ))}
        </nav>

        <div className="workspace-user">
          <div className="workspace-user__avatar" aria-hidden="true">
            {auth.auth?.user.username.slice(0, 1).toUpperCase()}
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
            <span>LIVE</span>
          </div>
          <span className="workspace-topbar__market">CN · ASIA/SHANGHAI</span>
        </header>
        <Outlet />
      </div>
    </div>
  )
}
