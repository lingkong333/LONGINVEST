import { isRouteErrorResponse } from "react-router-dom"

import { ApiError, createClientRequestId } from "@/shared/api/client"

export interface ErrorDiagnostic {
  code: string
  requestId: string
}

function requestIdFromRouteData(data: unknown) {
  if (!data || typeof data !== "object") {
    return undefined
  }
  const requestId = (data as { request_id?: unknown }).request_id
  return typeof requestId === "string" ? requestId : undefined
}

export function toErrorDiagnostic(error: unknown): ErrorDiagnostic {
  if (error instanceof ApiError) {
    return {
      code: error.code,
      requestId: error.requestId ?? createClientRequestId(),
    }
  }

  if (isRouteErrorResponse(error)) {
    return {
      code: `ROUTE_HTTP_${error.status}`,
      requestId: requestIdFromRouteData(error.data) ?? createClientRequestId(),
    }
  }

  return {
    code: "UNEXPECTED_CLIENT_ERROR",
    requestId: createClientRequestId(),
  }
}
