import { createContext, useContext } from "react"

export type Palette = "industrial" | "warm" | "candy"

export interface AppearanceContextValue {
  palette: Palette
  setPalette: (palette: Palette) => void
}

export const AppearanceContext = createContext<AppearanceContextValue | null>(null)

export function useAppearance() {
  const context = useContext(AppearanceContext)
  if (!context) {
    throw new Error("useAppearance 必须在 AppearanceProvider 中使用")
  }
  return context
}
