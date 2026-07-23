import { beforeEach, describe, expect, it } from "vitest"

import {
  readLastVisitedPage,
  readSidebarOpen,
  writeLastVisitedPage,
  writeSidebarOpen,
} from "@/app/client-preferences"

describe("客户端无敏感偏好", () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  it("首次访问时默认展开侧栏", () => {
    expect(readSidebarOpen()).toBe(true)
  })

  it("保存并恢复侧栏展开状态", () => {
    writeSidebarOpen(true)
    expect(readSidebarOpen()).toBe(true)

    writeSidebarOpen(false)
    expect(readSidebarOpen()).toBe(false)
  })

  it("保存最后访问页面及查询条件", () => {
    writeLastVisitedPage("/market-data/stocks/600519.SH", "?range=1y")

    expect(readLastVisitedPage()).toBe(
      "/market-data/stocks/600519.SH?range=1y",
    )
  })

  it("拒绝登录页、外部地址和未知页面", () => {
    window.localStorage.setItem("longinvest-last-page", "https://example.com")
    expect(readLastVisitedPage()).toBe("/")

    writeLastVisitedPage("/login")
    writeLastVisitedPage("/unknown")
    expect(readLastVisitedPage()).toBe("/")
  })
})
