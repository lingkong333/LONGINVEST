import type { QueryClient, QueryKey } from "@tanstack/react-query"

const RESOURCE_QUERY_KEYS = {
  jobs: [
    ["jobs"],
    ["dashboard"],
    ["monitoring"],
    ["system-status"],
  ],
  notifications: [
    ["notifications"],
    ["dashboard"],
  ],
  quote_cycles: [
    ["market-data"],
    ["dashboard"],
    ["monitoring"],
    ["system-status"],
  ],
  signals: [
    ["signals"],
    ["dashboard"],
    ["monitoring"],
  ],
  providers: [
    ["providers"],
    ["dashboard"],
    ["system-status"],
  ],
  alerts: [
    ["alerts"],
    ["dashboard"],
    ["monitoring"],
    ["system-status"],
  ],
  settings: [
    ["settings"],
    ["notifications"],
    ["providers"],
    ["dashboard"],
  ],
} as const satisfies Record<string, readonly QueryKey[]>

export type ResourceType = keyof typeof RESOURCE_QUERY_KEYS

export interface ResourceChangedEvent {
  resourceType: ResourceType
  resourceId: string
  version: number
  topic: string
}

export const FALLBACK_QUERY_KEYS = [
  ["jobs"],
  ["notifications"],
  ["market-data"],
  ["signals"],
  ["providers"],
  ["alerts"],
  ["settings"],
  ["dashboard"],
  ["monitoring"],
  ["system-status"],
] as const satisfies readonly QueryKey[]

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null
}

function isResourceType(value: unknown): value is ResourceType {
  return typeof value === "string" && value in RESOURCE_QUERY_KEYS
}

export function parseResourceChangedEvent(data: string): ResourceChangedEvent | null {
  let value: unknown
  try {
    value = JSON.parse(data)
  } catch {
    return null
  }
  if (
    !isRecord(value)
    || !isResourceType(value.resource_type)
    || typeof value.resource_id !== "string"
    || value.resource_id.length === 0
    || typeof value.version !== "number"
    || !Number.isSafeInteger(value.version)
    || value.version < 1
    || typeof value.topic !== "string"
    || value.topic.length === 0
  ) {
    return null
  }
  return {
    resourceType: value.resource_type,
    resourceId: value.resource_id,
    version: value.version,
    topic: value.topic,
  }
}

async function invalidateKeys(queryClient: QueryClient, keys: readonly QueryKey[]) {
  await Promise.allSettled(keys.map((queryKey) => queryClient.invalidateQueries({
    queryKey,
    refetchType: "active",
  })))
}

export function invalidateResourceQueries(
  queryClient: QueryClient,
  event: ResourceChangedEvent,
) {
  return invalidateKeys(queryClient, RESOURCE_QUERY_KEYS[event.resourceType])
}

export function refreshActiveFallbackQueries(queryClient: QueryClient) {
  return invalidateKeys(queryClient, FALLBACK_QUERY_KEYS)
}
