import {
  EventStreamContentType,
  fetchEventSource,
  type EventSourceMessage,
} from "@microsoft/fetch-event-source"

import {
  parseResourceChangedEvent,
  type ResourceChangedEvent,
} from "@/shared/realtime/resource-events"

export type ResourceEventConnectionState = "connected" | "reconnecting"

export interface ResourceEventConnectionOptions {
  signal: AbortSignal
  onEvent(event: ResourceChangedEvent): void
  onStateChange(state: ResourceEventConnectionState): void
  onUnauthorized(): void
}

export type ResourceEventConnector = (
  options: ResourceEventConnectionOptions,
) => Promise<void>

class FatalStreamError extends Error {}
class ResetCursorError extends Error {}
class UnauthorizedStreamError extends Error {}

const MAX_RETRY_DELAY_MS = 30_000

function retryDelay(attempt: number) {
  return Math.min(1_000 * (2 ** attempt), MAX_RETRY_DELAY_MS)
}

function isEventStream(response: Response) {
  return response.headers.get("content-type")?.startsWith(EventStreamContentType)
}

function parseMessage(message: EventSourceMessage) {
  if (message.event !== "resource.changed") {
    return null
  }
  return parseResourceChangedEvent(message.data)
}

export const connectResourceEventStream: ResourceEventConnector = async ({
  signal,
  onEvent,
  onStateChange,
  onUnauthorized,
}) => {
  let cursorWasReset = false

  while (!signal.aborted) {
    let retryAttempt = 0
    try {
      await fetchEventSource("/api/v1/events/stream", {
        credentials: "include",
        signal,
        openWhenHidden: false,
        async onopen(response) {
          if (response.ok && isEventStream(response)) {
            retryAttempt = 0
            onStateChange("connected")
            return
          }
          if (response.status === 401) {
            throw new UnauthorizedStreamError()
          }
          if (response.status === 409) {
            throw new ResetCursorError()
          }
          if (response.status >= 400 && response.status < 500 && response.status !== 429) {
            throw new FatalStreamError()
          }
          throw new Error(`SSE connection failed with HTTP ${response.status}`)
        },
        onmessage(message) {
          const event = parseMessage(message)
          if (event) {
            onEvent(event)
          }
        },
        onclose() {
          onStateChange("reconnecting")
          throw new Error("SSE connection closed")
        },
        onerror(error) {
          if (
            error instanceof FatalStreamError
            || error instanceof ResetCursorError
            || error instanceof UnauthorizedStreamError
          ) {
            throw error
          }
          onStateChange("reconnecting")
          const delay = retryDelay(retryAttempt)
          retryAttempt += 1
          return delay
        },
      })
      return
    } catch (error) {
      if (signal.aborted) {
        return
      }
      if (error instanceof UnauthorizedStreamError) {
        onUnauthorized()
        return
      }
      if (error instanceof ResetCursorError && !cursorWasReset) {
        cursorWasReset = true
        continue
      }
      throw error
    }
  }
}
