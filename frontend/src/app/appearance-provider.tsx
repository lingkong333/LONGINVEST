import { ThemeProvider as ColorModeProvider } from "next-themes"
import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react"

import {
  AppearanceContext,
  type Palette,
} from "@/app/appearance-context"

const PALETTE_STORAGE_KEY = "longinvest-palette"
const palettes = new Set<Palette>(["industrial", "warm", "candy"])

function storedPalette(): Palette {
  try {
    const stored = window.localStorage.getItem(PALETTE_STORAGE_KEY)
    return palettes.has(stored as Palette) ? stored as Palette : "industrial"
  } catch {
    return "industrial"
  }
}

export function AppearanceProvider({ children }: { children: ReactNode }) {
  const [palette, setPaletteState] = useState<Palette>(storedPalette)

  useEffect(() => {
    document.documentElement.dataset.theme = palette
    try {
      window.localStorage.setItem(PALETTE_STORAGE_KEY, palette)
    } catch {
      // Browsers with disabled storage still receive the selected theme for this session.
    }
  }, [palette])

  const setPalette = useCallback((value: Palette) => {
    setPaletteState(value)
  }, [])
  const value = useMemo(() => ({ palette, setPalette }), [palette, setPalette])

  return (
    <ColorModeProvider
      attribute="class"
      defaultTheme="light"
      enableSystem={false}
      disableTransitionOnChange
      storageKey="longinvest-color-mode"
    >
      <AppearanceContext.Provider value={value}>
        {children}
      </AppearanceContext.Provider>
    </ColorModeProvider>
  )
}
