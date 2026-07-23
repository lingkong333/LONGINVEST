import {
  Activity,
  BellRing,
  BriefcaseBusiness,
  CalendarDays,
  CandlestickChart,
  ChartNoAxesCombined,
  CircleGauge,
  FlaskConical,
  HeartPulse,
  LogOut,
  Radar,
  RadioTower,
  ScanSearch,
  ScrollText,
  Server,
  Settings2,
  ShieldAlert,
  Target,
  type LucideIcon,
} from "lucide-react"
import { NavLink, Outlet, useLocation } from "react-router-dom"

import { AppearanceMenu } from "@/app/appearance-menu"
import { useAuth } from "@/features/auth"
import { Button } from "@/shared/ui/button"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarRail,
  SidebarTrigger,
} from "@/shared/ui/sidebar"

interface NavigationItem {
  label: string
  path: string
  icon: LucideIcon
  exact?: boolean
}

const navigation: NavigationItem[] = [
  { label: "仪表盘", path: "/", icon: CircleGauge, exact: true },
  { label: "监控列表", path: "/monitoring", icon: Radar },
  { label: "持仓管理", path: "/positions", icon: BriefcaseBusiness },
  { label: "目标价管理", path: "/targets", icon: Target },
  { label: "信号中心", path: "/signals", icon: RadioTower },
  { label: "策略工作台", path: "/strategies", icon: FlaskConical },
  { label: "回测任务", path: "/backtests", icon: ChartNoAxesCombined },
  { label: "行情数据中心", path: "/market-data", icon: ScanSearch },
  { label: "通知中心", path: "/notifications", icon: BellRing },
  { label: "任务管理", path: "/jobs", icon: Activity },
  { label: "数据源管理", path: "/providers", icon: Server },
  { label: "系统告警", path: "/alerts", icon: ShieldAlert },
  { label: "交易日历", path: "/calendar", icon: CalendarDays },
  { label: "运行状态", path: "/system-status", icon: HeartPulse },
  { label: "审计记录", path: "/audit", icon: ScrollText },
  { label: "系统设置", path: "/settings", icon: Settings2 },
]

export function AppShell() {
  const auth = useAuth()
  const location = useLocation()

  return (
    <SidebarProvider defaultOpen={false}>
      <Sidebar collapsible="icon">
        <SidebarHeader>
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton size="lg" asChild tooltip="LongInvest">
                <NavLink to="/">
                  <CandlestickChart aria-hidden="true" />
                  <span className="font-semibold">LongInvest</span>
                </NavLink>
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
        </SidebarHeader>

        <SidebarContent>
          <SidebarGroup>
            <SidebarGroupContent>
              <SidebarMenu>
                {navigation.map((item) => {
                  const Icon = item.icon
                  const isActive = item.exact
                    ? location.pathname === item.path
                    : location.pathname.startsWith(item.path)
                  return (
                    <SidebarMenuItem key={item.path}>
                      <SidebarMenuButton asChild tooltip={item.label} isActive={isActive}>
                        <NavLink to={item.path} end={item.exact}>
                          <Icon aria-hidden="true" />
                          <span>{item.label}</span>
                        </NavLink>
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                  )
                })}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        </SidebarContent>

        <SidebarFooter>
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton tooltip={auth.auth?.user.username ?? "管理员"}>
                <span className="flex size-6 items-center justify-center rounded-md bg-sidebar-primary text-xs font-semibold text-sidebar-primary-foreground">
                  {auth.auth?.user.username.slice(0, 1).toUpperCase()}
                </span>
                <span className="truncate">{auth.auth?.user.username}</span>
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => void auth.logout()}
            disabled={auth.isSubmitting}
          >
            <LogOut data-icon="inline-start" aria-hidden="true" />
            退出登录
          </Button>
        </SidebarFooter>
        <SidebarRail />
      </Sidebar>

      <SidebarInset>
        <header className="sticky top-0 z-10 flex h-12 shrink-0 items-center justify-between border-b bg-background/95 px-3 backdrop-blur">
          <div className="flex items-center gap-2">
            <SidebarTrigger />
            <span className="inline-flex items-center gap-2 text-sm text-muted-foreground">
              <span className="size-2 rounded-full bg-primary" aria-hidden="true" />
              运行中
            </span>
          </div>
          <div className="flex items-center gap-2">
            <span className="hidden text-xs text-muted-foreground sm:inline">
              中国 A 股 · 上海时间
            </span>
            <AppearanceMenu />
          </div>
        </header>
        <div className="min-w-0 flex-1">
          <Outlet />
        </div>
      </SidebarInset>
    </SidebarProvider>
  )
}
