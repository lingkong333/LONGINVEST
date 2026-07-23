import type { components, paths } from "@/shared/api/generated/schema"
import { createApiClient, createClientIdempotencyKey } from "@/shared/api/client"

import type {
  CalculateTargetInput,
  ManualTargetInput,
  RestoreTargetInput,
  ReviewDecisionInput,
  TargetAction,
  TargetManagementApi,
  TargetReviewItem,
} from "./types"

type ApiClient = ReturnType<typeof createApiClient<paths>>

const targetActions = new Set<TargetAction>([
  "MANUAL_EDIT",
  "CALCULATE",
  "RETRY",
  "RESTORE",
  "APPROVE",
  "REJECT",
  "RECALCULATE",
])

function idempotencyKey() {
  return createClientIdempotencyKey()
}

function recordOf(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null ? value as Record<string, unknown> : {}
}

function allowedActions(value: unknown): TargetAction[] {
  const source = recordOf(value).allowed_actions
  if (!Array.isArray(source)) return []
  return source.filter((action): action is TargetAction =>
    typeof action === "string" && targetActions.has(action as TargetAction),
  )
}

function targetItem<T extends components["schemas"]["TargetRecord"]>(
  value: T,
): T & { allowedActions: TargetAction[] } {
  return { ...value, allowedActions: allowedActions(value) }
}

export function createTargetManagementApi(api = createApiClient<paths>()): TargetManagementApi {
  return {
    async listTargets() {
      const data = await api.request<components["schemas"]["TargetPageData"]>(
        api.client.GET("/api/v1/targets", {
        params: { query: { page: 1, page_size: 200 } },
        }),
      )
      return data.items.map(targetItem)
    },
    async getTarget(subscriptionId) {
      const data = await api.request<components["schemas"]["TargetRecord"]>(
        api.client.GET("/api/v1/targets/{subscription_id}", {
          params: { path: { subscription_id: subscriptionId } },
        }),
      )
      return targetItem(data)
    },
    async listHistory(subscriptionId) {
      const data = await api.request<components["schemas"]["TargetHistoryData"]>(
        api.client.GET("/api/v1/targets/{subscription_id}/history", {
          params: { path: { subscription_id: subscriptionId }, query: { page: 1, page_size: 200 } },
        }),
      )
      return data.items
    },
    async listRuns() {
      const data = await api.request<components["schemas"]["CalculationRunPageData"]>(
        api.client.GET("/api/v1/target-calculation-runs", {
          params: { query: { page: 1, page_size: 200 } },
        }),
      )
      return data.items
    },
    async listReviews() {
      const data = await api.request<components["schemas"]["ReviewPageData"]>(
        api.client.GET("/api/v1/target-reviews", {
          params: { query: { page: 1, page_size: 200 } },
        }),
      )
      return data.items.map((review): TargetReviewItem => ({
        ...review,
        allowedActions: allowedActions(review),
        version: review.binding_version,
        baseline: review.baseline,
        candidate: review.candidate,
      }))
    },
    async setManual(subscriptionId, input: ManualTargetInput) {
      await api.request(api.client.POST("/api/v1/targets/{subscription_id}/manual", {
        params: {
          path: { subscription_id: subscriptionId },
          header: { "Idempotency-Key": idempotencyKey() },
        },
        body: {
          confirm: true,
          target_date: input.targetDate,
          values: input.values,
          reason: input.reason,
          expected_version: input.expectedVersion,
          large_change_confirmed: input.largeChangeConfirmed,
          switch_to_manual_confirmed: input.switchToManualConfirmed,
        },
      }))
    },
    async calculate(subscriptionId, input: CalculateTargetInput) {
      await api.request(api.client.POST("/api/v1/targets/{subscription_id}/calculate", {
        params: {
          path: { subscription_id: subscriptionId },
          header: { "Idempotency-Key": idempotencyKey() },
        },
        body: {
          confirm: true,
          target_date: input.targetDate,
          training_start_date: input.trainingStartDate,
          training_end_date: input.trainingEndDate,
          reason: input.reason,
          expected_version: input.expectedVersion,
        },
      }))
    },
    async retry(subscriptionId, reason, expectedVersion) {
      await api.request(api.client.POST("/api/v1/targets/{subscription_id}/retry", {
        params: {
          path: { subscription_id: subscriptionId },
          header: { "Idempotency-Key": idempotencyKey() },
        },
        body: { confirm: true, reason, expected_version: expectedVersion },
      }))
    },
    async restore(subscriptionId, input: RestoreTargetInput) {
      await api.request(api.client.POST("/api/v1/targets/{subscription_id}/restore", {
        params: {
          path: { subscription_id: subscriptionId },
          header: { "Idempotency-Key": idempotencyKey() },
        },
        body: {
          confirm: true,
          source_revision_id: input.sourceRevisionId,
          reason: input.reason,
          expected_version: input.expectedVersion,
          switch_to_manual_confirmed: input.switchToManualConfirmed,
        },
      }))
    },
    async approve(reviewId, input: ReviewDecisionInput) {
      await decideReview(api, reviewId, "approve", input)
    },
    async reject(reviewId, input: ReviewDecisionInput) {
      await decideReview(api, reviewId, "reject", input)
    },
    async recalculate(reviewId, reason, expectedVersion) {
      await api.request(api.client.POST("/api/v1/target-reviews/{review_id}/recalculate", {
        params: {
          path: { review_id: reviewId },
          header: { "Idempotency-Key": idempotencyKey() },
        },
        body: { confirm: true, reason, expected_version: expectedVersion },
      }))
    },
  }
}

async function decideReview(
  api: ApiClient,
  reviewId: string,
  decision: "approve" | "reject",
  input: ReviewDecisionInput,
) {
  const request = {
    params: {
      path: { review_id: reviewId },
      header: { "Idempotency-Key": idempotencyKey() },
    },
    body: {
      confirm: true,
      reason: input.comment,
      comment: input.comment,
      expected_version: input.expectedVersion,
    },
  } as const
  if (decision === "approve") {
    await api.request(api.client.POST("/api/v1/target-reviews/{review_id}/approve", request))
    return
  }
  await api.request(api.client.POST("/api/v1/target-reviews/{review_id}/reject", request))
}

export const targetGatewayInternals = { allowedActions }
