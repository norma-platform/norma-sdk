// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import { remap as remap$ } from "../lib/primitives.js";
import { Agent, Agent$Outbound, Agent$outboundSchema } from "./agent.js";
import { ConflictMode, ConflictMode$outboundSchema } from "./conflict-mode.js";
import {
  EvaluatorSchema,
  EvaluatorSchema$Outbound,
  EvaluatorSchema$outboundSchema,
} from "./evaluator-schema.js";
import {
  StepSchema,
  StepSchema$Outbound,
  StepSchema$outboundSchema,
} from "./step-schema.js";

/**
 * Request to initialize or update an agent registration.
 */
export type RegisterAgentRequest = {
  /**
   * Agent metadata for registration and tracking.
   *
   * @remarks
   *
   * An agent represents an AI system that can be protected and monitored.
   * Each agent has a unique immutable name and can have multiple steps registered with it.
   */
  agent: Agent;
  /**
   * Conflict handling mode for register registration updates.
   *
   * @remarks
   *
   * STRICT preserves compatibility checks and raises conflicts on incompatible changes.
   * OVERWRITE applies latest-init-wins replacement for steps and evaluators.
   */
  conflictMode?: ConflictMode | undefined;
  /**
   * Custom evaluator schemas for config validation
   */
  evaluators?: Array<EvaluatorSchema> | undefined;
  /**
   * If true, replace corrupted agent data instead of failing. Use only when agent data is corrupted and cannot be parsed.
   */
  forceReplace?: boolean | undefined;
  /**
   * List of steps available to the agent
   */
  steps?: Array<StepSchema> | undefined;
  /**
   * Optional opaque target identifier. Required when target_type is supplied.
   */
  targetId?: string | null | undefined;
  /**
   * Optional opaque target kind. When supplied with target_id, the returned rules include rules bound to that target via rule bindings, in addition to the agent's direct and policy-derived rules.
   */
  targetType?: string | null | undefined;
};

/** @internal */
export type RegisterAgentRequest$Outbound = {
  agent: Agent$Outbound;
  conflict_mode?: string | undefined;
  evaluators?: Array<EvaluatorSchema$Outbound> | undefined;
  force_replace: boolean;
  steps?: Array<StepSchema$Outbound> | undefined;
  target_id?: string | null | undefined;
  target_type?: string | null | undefined;
};

/** @internal */
export const RegisterAgentRequest$outboundSchema: z.ZodMiniType<
  RegisterAgentRequest$Outbound,
  RegisterAgentRequest
> = z.pipe(
  z.object({
    agent: Agent$outboundSchema,
    conflictMode: z._default(z.optional(ConflictMode$outboundSchema), "strict"),
    evaluators: z.optional(z.array(EvaluatorSchema$outboundSchema)),
    forceReplace: z._default(z.boolean(), false),
    steps: z.optional(z.array(StepSchema$outboundSchema)),
    targetId: z.optional(z.nullable(z.string())),
    targetType: z.optional(z.nullable(z.string())),
  }),
  z.transform((v) => {
    return remap$(v, {
      conflictMode: "conflict_mode",
      forceReplace: "force_replace",
      targetId: "target_id",
      targetType: "target_type",
    });
  }),
);

export function registerRequestToJSON(
  registerRequest: RegisterAgentRequest,
): string {
  return JSON.stringify(
    RegisterAgentRequest$outboundSchema.parse(registerRequest),
  );
}
