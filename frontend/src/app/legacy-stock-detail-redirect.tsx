import { Navigate, useParams } from "react-router-dom"

export function LegacyStockDetailRedirect() {
  const { symbol = "" } = useParams<{ symbol: string }>()
  return <Navigate to={`/stocks/${encodeURIComponent(symbol)}`} replace />
}
