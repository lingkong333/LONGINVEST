import { Moon, Palette, Sun } from "lucide-react"
import { useTheme } from "next-themes"

import {
  useAppearance,
  type Palette as PaletteName,
} from "@/app/appearance-context"
import { Button } from "@/shared/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/shared/ui/dropdown-menu"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/shared/ui/tooltip"

const paletteOptions: Array<{
  value: PaletteName
  label: string
  colors: [string, string, string]
}> = [
  { value: "industrial", label: "工业红灰", colors: ["#b71c1c", "#556b2f", "#4682b4"] },
  { value: "warm", label: "暖棕", colors: ["#a37764", "#baab92", "#e4c7b8"] },
  { value: "candy", label: "糖果", colors: ["#d04f99", "#8acfd1", "#fbe2a7"] },
]

export function AppearanceMenu() {
  const { palette, setPalette } = useAppearance()
  const { theme, setTheme } = useTheme()
  const isDark = theme === "dark"

  return (
    <TooltipProvider>
      <div className="flex items-center gap-1">
      <DropdownMenu>
        <Tooltip>
          <TooltipTrigger asChild>
            <DropdownMenuTrigger asChild>
              <Button type="button" variant="ghost" size="icon-sm" aria-label="选择配色主题">
                <Palette aria-hidden="true" />
              </Button>
            </DropdownMenuTrigger>
          </TooltipTrigger>
          <TooltipContent>配色主题</TooltipContent>
        </Tooltip>
        <DropdownMenuContent align="end" className="w-52">
          <DropdownMenuGroup>
            <DropdownMenuLabel>配色主题</DropdownMenuLabel>
            <DropdownMenuRadioGroup
              value={palette}
              onValueChange={(value) => setPalette(value as PaletteName)}
            >
              {paletteOptions.map((option) => (
                <DropdownMenuRadioItem key={option.value} value={option.value}>
                  <span className="flex items-center gap-1" aria-hidden="true">
                    {option.colors.map((color) => (
                      <span
                        key={color}
                        className="size-3 rounded-full border"
                        style={{ backgroundColor: color }}
                      />
                    ))}
                  </span>
                  <span className="flex-1">{option.label}</span>
                </DropdownMenuRadioItem>
              ))}
            </DropdownMenuRadioGroup>
          </DropdownMenuGroup>
          <DropdownMenuSeparator />
          <DropdownMenuLabel>当前模式：{isDark ? "暗色" : "亮色"}</DropdownMenuLabel>
        </DropdownMenuContent>
      </DropdownMenu>

      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label={isDark ? "切换亮色模式" : "切换暗色模式"}
            onClick={() => setTheme(isDark ? "light" : "dark")}
          >
            {isDark ? <Sun aria-hidden="true" /> : <Moon aria-hidden="true" />}
          </Button>
        </TooltipTrigger>
        <TooltipContent>{isDark ? "切换亮色模式" : "切换暗色模式"}</TooltipContent>
      </Tooltip>
      </div>
    </TooltipProvider>
  )
}
