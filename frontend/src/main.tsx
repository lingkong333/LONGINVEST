import { StrictMode } from "react"
import { createRoot } from "react-dom/client"

import { App } from "@/app/app"
import "@/styles.css"

const rootElement = document.getElementById("root")

if (!rootElement) {
  throw new Error("缺少应用挂载节点")
}

createRoot(rootElement).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
