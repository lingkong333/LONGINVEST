import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { useTheme } from "next-themes"
import { beforeEach, describe, expect, it } from "vitest"

import { useAppearance } from "@/app/appearance-context"
import { AppearanceProvider } from "@/app/appearance-provider"

function AppearanceProbe() {
  const { palette, setPalette } = useAppearance()
  const { theme, setTheme } = useTheme()

  return (
    <div>
      <output aria-label="当前配色">{palette}</output>
      <output aria-label="当前模式">{theme}</output>
      <button type="button" onClick={() => setPalette("warm")}>
        使用暖棕
      </button>
      <button type="button" onClick={() => setTheme("dark")}>
        使用暗色
      </button>
    </div>
  )
}

function renderAppearance() {
  return render(
    <AppearanceProvider>
      <AppearanceProbe />
    </AppearanceProvider>,
  )
}

describe("外观设置", () => {
  beforeEach(() => {
    window.localStorage.clear()
    document.documentElement.className = ""
    delete document.documentElement.dataset.theme
  })

  it("切换配色和亮暗模式后立即生效并保存", async () => {
    const user = userEvent.setup()
    renderAppearance()

    expect(screen.getByLabelText("当前配色")).toHaveTextContent("industrial")

    await user.click(screen.getByRole("button", { name: "使用暖棕" }))
    await waitFor(() => {
      expect(document.documentElement.dataset.theme).toBe("warm")
      expect(window.localStorage.getItem("longinvest-palette")).toBe("warm")
    })

    await user.click(screen.getByRole("button", { name: "使用暗色" }))
    await waitFor(() => {
      expect(document.documentElement).toHaveClass("dark")
      expect(window.localStorage.getItem("longinvest-color-mode")).toBe("dark")
    })
  })

  it("启动时恢复上一次选择的配色", async () => {
    window.localStorage.setItem("longinvest-palette", "candy")
    renderAppearance()

    expect(screen.getByLabelText("当前配色")).toHaveTextContent("candy")
    await waitFor(() => {
      expect(document.documentElement.dataset.theme).toBe("candy")
    })
  })
})
