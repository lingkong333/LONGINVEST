import type { components } from "@/shared/api/generated/schema"

export type TargetSnapshot = components["schemas"]["TargetRecord"]
export type TargetRevision = components["schemas"]["TargetRevisionView"]
export type TargetRun = components["schemas"]["TargetCalculationRunView"]
export type TargetReview = components["schemas"]["TargetReviewDetail"]
export type TargetValues = components["schemas"]["TargetValues-Output"]

export type TargetAction =
  | "MANUAL_EDIT"
  | "CALCULATE"
  | "RETRY"
  | "RESTORE"
  | "APPROVE"
  | "REJECT"
  | "RECALCULATE"

export interface TargetItem extends TargetSnapshot {
  allowedActions: TargetAction[]
}

export type TargetReviewItem = TargetReview & {
  allowedActions: TargetAction[]
  version: number
}

export interface ManualTargetInput {
  targetDate: string
  values: TargetValues
  reason: string
  expectedVersion: number
  largeChangeConfirmed: boolean
  switchToManualConfirmed: boolean
}

export interface CalculateTargetInput {
  targetDate: string
  trainingStartDate: string
  trainingEndDate: string
  reason: string
  expectedVersion: number
}

export interface RestoreTargetInput {
  sourceRevisionId: string
  reason: string
  expectedVersion: number
  switchToManualConfirmed: boolean
}

export interface ReviewDecisionInput {
  comment: string
  expectedVersion: number
}

export interface TargetManagementApi {
  listTargets(): Promise<TargetItem[]>
  getTarget(subscriptionId: string): Promise<TargetItem>
  listHistory(subscriptionId: string): Promise<TargetRevision[]>
  listRuns(): Promise<TargetRun[]>
  listReviews(): Promise<TargetReviewItem[]>
  setManual(subscriptionId: string, input: ManualTargetInput): Promise<void>
  calculate(subscriptionId: string, input: CalculateTargetInput): Promise<void>
  retry(subscriptionId: string, reason: string, expectedVersion: number): Promise<void>
  restore(subscriptionId: string, input: RestoreTargetInput): Promise<void>
  approve(reviewId: string, input: ReviewDecisionInput): Promise<void>
  reject(reviewId: string, input: ReviewDecisionInput): Promise<void>
  recalculate(reviewId: string, reason: string, expectedVersion: number): Promise<void>
}
