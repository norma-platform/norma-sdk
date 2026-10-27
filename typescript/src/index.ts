// Modified for Norma.
import { defaultClient } from "./client";

export { NormaClient } from "./client";
export { UnsupportedControlContractError } from "./control-contract";
export type { ControlContract } from "./control-contract";
export { checkpoint } from "./checkpoint";
export { checkFinish, TurnRecorder } from "./finish";
export type { ActionSummary, FinishVerdict, TurnRecord, RecordAction, TurnRecorderOptions } from "./finish";
export type { ActionKind, NormaAction, CheckpointOptions } from "./checkpoint";
export type {
  CheckOutcome, SemanticAnswer, SemanticObservation, SemanticEvidenceItem,
  SemanticEvidenceBundle, RecoveryFeedback, RecoveryEvidenceReference,
} from "./internal/semantic-check";
export { CheckError, RuleViolationError, RuleSteerError } from "./errors";
export type { RuleFailureContext } from "./errors";
export type {
  NormaInitOptions,
  NormaConfig,
  AgentsApi,
  EvaluatorsApi,
  ObservabilityApi,
  StepSchema,
} from "./client";
export type { JsonObject, JsonPrimitive, JsonValue } from "./types";
export * from "./types";

export default defaultClient;

export { HTTPClient } from "./generated/lib/http";
export { NormaAPIError } from "./operations";
export type { ControlRequestDiagnostic, ControlDiagnosticCode } from "./api-error";
export type { OperationsApi, CreateOperation, ObserveOperation, OperationRecord, OperationObservation, OperationVerification, OperationRequestOptions } from "./operations";

export { semanticDefinition } from "./internal/semantic-definition";
export type { NormaSemanticDefinitionInput, NormaSemanticDefinition, NormaSemanticAnswer, NormaSemanticQuestion, NormaSemanticRule, SemanticStatementQuestion, SemanticStatementRule, SemanticStatementAnswer, SemanticScoreQuestion, SemanticScoreRule, SemanticScoreAnswer } from "./internal/semantic-definition";

export type { ChecksApi } from "./public-api";
export type { RuleDefinition, RuleScope, RuleAction, RuleMatch, CheckRequest, CheckResponse, CheckResult, Decision } from "./public-api";
export type { DeliveryReceipt } from "./check-events";

export type { Rule, RuleCondition, RuleResult, RuleBinding, RuleBindingsApi, RuleCheckEvent, RequestOptions } from "./public-api";
export type { ControlEvidenceSnapshot, ControlReceipt, ControlReferenceEvidence, NumericComparisonEvidence } from "./generated/models/control-evidence";

export { validateAuthoredControls } from "./controls";
export type {
  ControlApplicabilityOperator,
  ControlApplicabilityPredicate,
  BooleanControlDeclaration,
  ControlDeclaration,
  ControlDiagnostic,
  ControlEvaluatorManifest,
  ControlExpectedValueSchema,
  ControlReference,
  ControlSlotDescriptor,
  ControlSourceValidation,
  ControlStatus,
  ControlType,
  ControlValidationResult,
  EnumControlDeclaration,
  NumberControlDeclaration,
  StringControlDeclaration,
  StringListControlDeclaration,
  TrustedControlCapabilities,
} from "./controls";
