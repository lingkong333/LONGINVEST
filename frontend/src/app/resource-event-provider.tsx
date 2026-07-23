import { useQueryClient } from "@tanstack/react-query"
import { useEffect, useRef, type ReactNode } from "react"

import { useAuth } from "@/features/auth"
import {
  connectResourceEventStream,
  type ResourceEventConnector,
} from "@/shared/realtime/resource-event-stream"
import {
  invalidateResourceQueries,
  refreshActiveFallbackQueries,
} from "@/shared/realtime/resource-events"

const VISIBLE_FALLBACK_INTERVAL_MS = 30_000
const HIDDEN_FALLBACK_INTERVAL_MS = 120_000

export function ResourceEventProvider({
  children,
  connector = connectResourceEventStream,
}: {
  children: ReactNode
  connector?: ResourceEventConnector
}) {
  const { phase, invalidate: invalidateSession } = useAuth()
  const queryClient = useQueryClient()
  const latestVersion = useRef(0)

  useEffect(() => {
    if (phase !== "authenticated") {
      latestVersion.current = 0
      return
    }

    const controller = new AbortController()
    let fallbackTimer: number | undefined
    let reconnecting = false

    const stopFallback = () => {
      if (fallbackTimer !== undefined) {
        window.clearInterval(fallbackTimer)
        fallbackTimer = undefined
      }
    }

    const startFallback = () => {
      stopFallback()
      if (!reconnecting) {
        return
      }
      const interval = document.visibilityState === "hidden"
        ? HIDDEN_FALLBACK_INTERVAL_MS
        : VISIBLE_FALLBACK_INTERVAL_MS
      fallbackTimer = window.setInterval(() => {
        void refreshActiveFallbackQueries(queryClient)
      }, interval)
    }

    const onVisibilityChange = () => {
      if (reconnecting) {
        startFallback()
      }
    }
    document.addEventListener("visibilitychange", onVisibilityChange)

    void connector({
      signal: controller.signal,
      onEvent(event) {
        if (event.version <= latestVersion.current) {
          return
        }
        latestVersion.current = event.version
        void invalidateResourceQueries(queryClient, event)
      },
      onStateChange(state) {
        reconnecting = state === "reconnecting"
        if (reconnecting) {
          startFallback()
        } else {
          stopFallback()
        }
      },
      onUnauthorized: invalidateSession,
    }).catch(() => {
      if (!controller.signal.aborted) {
        reconnecting = true
        startFallback()
      }
    })

    return () => {
      controller.abort()
      stopFallback()
      document.removeEventListener("visibilitychange", onVisibilityChange)
    }
  }, [connector, invalidateSession, phase, queryClient])

  return children
}
