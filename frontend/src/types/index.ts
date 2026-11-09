/**
 * Barrel for all API types. Each module mirrors one backend Pydantic
 * schema module — keep them aligned with the backend; never invent fields.
 */
export type { Product } from "./product";
export type {
  Authorization,
  HardConstraints,
  Preferences,
  ProductIntent,
} from "./intent";
export type {
  Recommendation,
  RecommendResponse,
  RecommendStatus,
} from "./recommendation";
export type { PolicyDecision, PolicyOutcome, ReasonCode } from "./policy";
export type { PlanResponse, PlanStatus } from "./plan";
export type {
  RefundResult,
  TransactionListResponse,
  TransactionRecord,
  TransactionStatus,
} from "./transaction";
export type {
  ConversationTurn,
  PendingAction,
  PendingActionType,
  SessionDetail,
  TurnRole,
} from "./session";
export type { CaptureResult, ExecutionResult, ExecutionStatus } from "./execution";
export type {
  ChatData,
  ChatRequest,
  ChatResponse,
  ChatRoute,
} from "./chat";
export type { ConfirmRequest, ConfirmationResponse } from "./confirmation";
export type {
  ComparisonRequest,
  ComparisonRequirements,
  ComparisonResponse,
  ComparisonResult,
  NormalizedDiscoveredProduct,
  RankingMethod,
} from "./comparison";
export { CONFIGURED_DEMO_RATE } from "./comparison";
