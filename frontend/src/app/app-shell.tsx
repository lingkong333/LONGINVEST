import {
  Activity,
  BellRing,
  BriefcaseBusiness,
  CalendarDays,
  CandlestickChart,
  ChartNoAxesCombined,
  CircleGauge,
  FlaskConical,
  LogOut,
  Radar,
  RadioTower,
  Settings2,
  ShieldAlert,
  Target,
} from "lucide-react"
import { NavLink, Outlet } from "react-router-dom"

import { useAuth } from "@/features/auth"
import { Button } from "@/shared/ui/button"

const futureNavigation = [
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
        <span className="workspace-brand__mark" title="LongInvest">
          <CandlestickChart aria-hidden="true" />
        </span>

        <nav className="workspace-nav" aria-label="主导航">
          <NavLink to="/" end aria-label="仪表盘" title="仪表盘">
            <CircleGauge aria-hidden="true" />
          </NavLink>
          <NavLink to="/monitoring" aria-label="监控列表" title="监控列表">
            <Radar aria-hidden="true" />
          </NavLink>
          <NavLink to="/positions" aria-label="持仓管理" title="持仓管理">
            <BriefcaseBusiness aria-hidden="true" />
          </NavLink>
          <NavLink to="/targets" aria-label="目标价管理" title="目标价管理">
            <Target aria-hidden="true" />
          </NavLink>
          <NavLink to="/signals" aria-label="信号中心" title="信号中心">
            <RadioTower aria-hidden="true" />
          </NavLink>
          {futureNavigation.map(({ label, icon: Icon }) => (
            <span
              className="workspace-nav__future"
              key={label}
              aria-disabled="true"
              aria-label={label}
              title={label}
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
            <span>运行中</span>
          </div>
          <span className="workspace-topbar__market">中国 A 股 · 上海时间</span>
        </header>
        <Outlet />
      </div>
    </div>
  )
}
