// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import { remap as remap$ } from "../../lib/primitives.js";
import { ClosedEnum } from "../../types/enums.js";

/**
 * Rendered-state filter. Default 'all' returns both rendered rules and unrendered template drafts.
 */
export const RenderedState = {
  Rendered: "rendered",
  Unrendered: "unrendered",
  All: "all",
} as const;
/**
 * Rendered-state filter. Default 'all' returns both rendered rules and unrendered template drafts.
 */
export type RenderedState = ClosedEnum<typeof RenderedState>;

/**
 * Enabled-state filter. Default 'all' returns both enabled and disabled associated rules. Unrendered template drafts are disabled, so combine with rendered_state='rendered' to exclude them.
 */
export const EnabledState = {
  Enabled: "enabled",
  Disabled: "disabled",
  All: "all",
} as const;
/**
 * Enabled-state filter. Default 'all' returns both enabled and disabled associated rules. Unrendered template drafts are disabled, so combine with rendered_state='rendered' to exclude them.
 */
export type EnabledState = ClosedEnum<typeof EnabledState>;

export type ListAgentRulesApiV1AgentsAgentNameRulesGetRequest = {
  agentName: string;
  /**
   * Rendered-state filter. Default 'all' returns both rendered rules and unrendered template drafts.
   */
  renderedState?: RenderedState | undefined;
  /**
   * Enabled-state filter. Default 'all' returns both enabled and disabled associated rules. Unrendered template drafts are disabled, so combine with rendered_state='rendered' to exclude them.
   */
  enabledState?: EnabledState | undefined;
  /**
   * Optional opaque target kind. When supplied with target_id, the response includes rules bound to that target via enabled bindings, in addition to the agent's direct and policy-derived rules.
   */
  targetType?: string | null | undefined;
  /**
   * Optional opaque target identifier. Required when target_type is supplied.
   */
  targetId?: string | null | undefined;
};

/** @internal */
export const RenderedState$outboundSchema: z.ZodMiniEnum<typeof RenderedState> =
  z.enum(RenderedState);

/** @internal */
export const EnabledState$outboundSchema: z.ZodMiniEnum<typeof EnabledState> = z
  .enum(EnabledState);

/** @internal */
export type ListAgentRulesApiV1AgentsAgentNameRulesGetRequest$Outbound = {
  agent_name: string;
  rendered_state: string;
  enabled_state: string;
  target_type?: string | null | undefined;
  target_id?: string | null | undefined;
};

/** @internal */
export const ListAgentRulesApiV1AgentsAgentNameRulesGetRequest$outboundSchema:
  z.ZodMiniType<
    ListAgentRulesApiV1AgentsAgentNameRulesGetRequest$Outbound,
    ListAgentRulesApiV1AgentsAgentNameRulesGetRequest
  > = z.pipe(
    z.object({
      agentName: z.string(),
      renderedState: z._default(RenderedState$outboundSchema, "all"),
      enabledState: z._default(EnabledState$outboundSchema, "all"),
      targetType: z.optional(z.nullable(z.string())),
      targetId: z.optional(z.nullable(z.string())),
    }),
    z.transform((v) => {
      return remap$(v, {
        agentName: "agent_name",
        renderedState: "rendered_state",
        enabledState: "enabled_state",
        targetType: "target_type",
        targetId: "target_id",
      });
    }),
  );

export function listAgentRulesApiV1AgentsAgentNameRulesGetRequestToJSON(
  listAgentRulesApiV1AgentsAgentNameRulesGetRequest:
    ListAgentRulesApiV1AgentsAgentNameRulesGetRequest,
): string {
  return JSON.stringify(
    ListAgentRulesApiV1AgentsAgentNameRulesGetRequest$outboundSchema
      .parse(listAgentRulesApiV1AgentsAgentNameRulesGetRequest),
  );
}
