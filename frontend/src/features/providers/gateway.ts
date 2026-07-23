import { z } from "zod"

import type {
  ProviderCircuit,
  ProviderGateway,
  ProviderHealth,
  ProviderSummary,
  QuoteDiagnostic,
} from "@/features/providers/types"
import {
  ApiError,
  createApiClient,
  createClientRequestId,
} from "@/shared/api/client"
import type { paths } from "@/shared/api/generated/schema"

const providerCodeSchema = z.enum(["EASTMONEY", "SINA"])
const actionSchema = z.enum([
  "UPDATE_SETTINGS",
  "PROBE",
  "RESET",
  "QUOTE_DIAGNOSTICS",
])
const capabilitySchema = z.object({
  capability: z.string().min(1),
  enabled: z.boolean(),
  priority: z.number().int().min(0),
  concurrency: z.number().int().min(1),
  rate_per_second: z.number().positive(),
  timeout_seconds: z.number().positive(),
  auto_switch: z.boolean(),
})
const providerSchema = z.object({
  provider_code: providerCodeSchema,
  version: z.number().int().nonnegative(),
  reason: z.string(),
  capabilities: z.array(capabilitySchema),
  allowed_actions: z.array(actionSchema).optional().default([]),
})
const healthSchema = z.object({
  capability: z.string().min(1),
  status: z.string().min(1),
  consecutive_failures: z.number().int().nonnegative(),
  last_success_at: z.string().nullable(),
  last_failure_at: z.string().nullable(),
  metrics: z.record(z.string(), z.unknown()),
})
const circuitSchema = z.object({
  id: z.string().uuid(),
  provider_code: providerCodeSchema,
  capability: z.string().min(1),
  state: z.enum(["CLOSED", "OPEN", "HALF_OPEN", "DISABLED"]),
  consecutive_failures: z.number().int().nonnegative(),
  cooldown_index: z.number().int().nonnegative(),
  opened_at: z.string().nullable(),
  allowed_actions: z.array(actionSchema).optional().default([]),
})
const diagnosticSchema = z.object({
  symbols: z.array(z.string()),
  sources: z.array(z.object({
    provider: providerCodeSchema,
    items: z.array(z.object({
      symbol: z.string(),
      price: z.string(),
      quote_time: z.string(),
    }).passthrough()),
    failures: z.array(z.object({
      symbol: z.string(),
      code: z.string(),
    }).passthrough()),
    batch_error_code: z.string().nullable(),
  })),
  comparisons: z.array(z.object({
    symbol: z.string(),
    status: z.enum(["MATCH", "CONFLICT", "INCOMPLETE"]),
    missing_sources: z.array(providerCodeSchema),
    differences: z.record(z.string(), z.unknown()),
  })),
})

function parse<T>(schema: z.ZodType<T>, value: unknown, code: string): T {
  const parsed = schema.safeParse(value)
  if (!parsed.success) {
    throw new ApiError("数据源接口返回的数据无法识别。", {
      code,
      cause: parsed.error,
    })
  }
  return parsed.data
}

function metric(metrics: Record<string, unknown>, key: string) {
  const value = metrics[key]
  return typeof value === "number" && Number.isFinite(value) ? value : null
}

export function createProviderGateway(baseUrl = ""): ProviderGateway {
  const api = createApiClient<paths>({ baseUrl })
  return {
    async loadProviders() {
      const values = parse(
        z.array(providerSchema),
        await api.request<unknown>(api.client.GET("/api/v1/providers")),
        "PROVIDER_LIST_INVALID",
      )
      return values.map((value): ProviderSummary => ({
        code: value.provider_code,
        version: value.version,
        reason: value.reason,
        capabilities: value.capabilities.map((capability) => ({
          capability: capability.capability,
          enabled: capability.enabled,
          priority: capability.priority,
          concurrency: capability.concurrency,
          ratePerSecond: capability.rate_per_second,
          timeoutSeconds: capability.timeout_seconds,
          autoSwitch: capability.auto_switch,
        })),
        allowedActions: value.allowed_actions,
      }))
    },
    async loadHealth(providerCode) {
      const values = parse(
        z.array(healthSchema),
        await api.request<unknown>(api.client.GET(
          "/api/v1/providers/{provider_code}/health",
          { params: { path: { provider_code: providerCode } } },
        )),
        "PROVIDER_HEALTH_INVALID",
      )
      return values.map((value): ProviderHealth => ({
        capability: value.capability,
        status: value.status,
        consecutiveFailures: value.consecutive_failures,
        lastSuccessAt: value.last_success_at,
        lastFailureAt: value.last_failure_at,
        successRate: metric(value.metrics, "success_rate"),
        p95LatencyMs: metric(value.metrics, "p95_latency_ms"),
        rateLimitWaitMs: metric(value.metrics, "rate_limit_wait_ms"),
        switchCount: metric(value.metrics, "switch_count"),
        schemaErrors: metric(value.metrics, "schema_errors"),
      }))
    },
    async loadCircuits() {
      const values = parse(
        z.array(circuitSchema),
        await api.request<unknown>(api.client.GET("/api/v1/providers/circuits")),
        "PROVIDER_CIRCUITS_INVALID",
      )
      return values.map((value): ProviderCircuit => ({
        id: value.id,
        providerCode: value.provider_code,
        capability: value.capability,
        state: value.state,
        consecutiveFailures: value.consecutive_failures,
        cooldownIndex: value.cooldown_index,
        openedAt: value.opened_at,
        allowedActions: value.allowed_actions,
      }))
    },
    async updateSettings(input) {
      await api.request(api.client.PATCH(
        "/api/v1/providers/{provider_code}/settings",
        {
          params: {
            path: { provider_code: input.provider.code },
            header: { "Idempotency-Key": createClientRequestId() },
          },
          body: {
            confirm: true,
            reason: input.reason,
            expected_version: input.provider.version,
            enabled: input.settings.enabled,
            priority: input.settings.priority,
            concurrency: input.settings.concurrency,
            rate_per_second: input.settings.ratePerSecond,
            timeout_seconds: input.settings.timeoutSeconds,
            auto_switch: input.settings.autoSwitch,
          },
        },
      ))
    },
    async runCircuitAction(input) {
      const request = {
        params: {
          path: { circuit_id: input.circuit.id },
          header: { "Idempotency-Key": createClientRequestId() },
        },
        body: { confirm: true as const, reason: input.reason },
      }
      const operation = input.action === "PROBE"
        ? api.client.POST(
            "/api/v1/providers/circuits/{circuit_id}/probe",
            request,
          )
        : api.client.POST(
            "/api/v1/providers/circuits/{circuit_id}/reset",
            request,
          )
      await api.request(operation)
    },
    async runQuoteDiagnostics(symbols, reason) {
      const value = parse(
        diagnosticSchema,
        await api.request<unknown>(api.client.POST(
          "/api/v1/providers/quote-diagnostics",
          {
            params: {
              header: { "Idempotency-Key": createClientRequestId() },
            },
            body: { confirm: true, reason, symbols },
          },
        )),
        "PROVIDER_DIAGNOSTIC_INVALID",
      )
      return {
        symbols: value.symbols,
        sources: value.sources.map((source) => ({
          provider: source.provider,
          items: source.items.map((item) => ({
            symbol: item.symbol,
            price: item.price,
            quoteTime: item.quote_time,
          })),
          failures: source.failures.map((failure) => ({
            symbol: failure.symbol,
            code: failure.code,
          })),
          batchErrorCode: source.batch_error_code,
        })),
        comparisons: value.comparisons.map((comparison) => ({
          symbol: comparison.symbol,
          status: comparison.status,
          missingSources: comparison.missing_sources,
        })),
      } satisfies QuoteDiagnostic
    },
  }
}

export const providerGateway = createProviderGateway()
