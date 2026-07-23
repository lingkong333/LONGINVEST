const SIDEBAR_OPEN_STORAGE_KEY = "longinvest-sidebar-open"
const LAST_PAGE_STORAGE_KEY = "longinvest-last-page"

const allowedPagePrefixes = [
  "/alerts",
  "/audit",
  "/backtests",
  "/calendar",
  "/jobs",
  "/market-data",
  "/monitoring",
  "/notifications",
  "/positions",
  "/providers",
  "/settings",
  "/signals",
  "/strategies",
  "/system-status",
  "/targets",
]

function isAllowedPage(value: string) {
  if (!value.startsWith("/") || value.startsWith("//") || value === "/login") {
    return false
  }
  try {
    const url = new URL(value, window.location.origin)
    if (url.origin !== window.location.origin || url.hash) {
      return false
    }
    return url.pathname === "/"
      || allowedPagePrefixes.some(
        (prefix) => url.pathname === prefix || url.pathname.startsWith(`${prefix}/`),
      )
  } catch {
    return false
  }
}

export function readSidebarOpen() {
  try {
    const stored = window.localStorage.getItem(SIDEBAR_OPEN_STORAGE_KEY)
    return stored === null ? true : stored === "true"
  } catch {
    return true
  }
}

export function writeSidebarOpen(open: boolean) {
  try {
    window.localStorage.setItem(SIDEBAR_OPEN_STORAGE_KEY, String(open))
  } catch {
    // The current session still works when browser storage is unavailable.
  }
}

export function readLastVisitedPage() {
  try {
    const stored = window.localStorage.getItem(LAST_PAGE_STORAGE_KEY)
    return stored && isAllowedPage(stored) ? stored : "/"
  } catch {
    return "/"
  }
}

export function writeLastVisitedPage(pathname: string, search = "") {
  const value = `${pathname}${search}`
  if (!isAllowedPage(value)) {
    return
  }
  try {
    window.localStorage.setItem(LAST_PAGE_STORAGE_KEY, value)
  } catch {
    // Navigation continues normally when browser storage is unavailable.
  }
}
