// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import { remap as remap$ } from "../../lib/primitives.js";

export type GetAgentEvaluatorApiV1AgentsAgentNameEvaluatorsEvaluatorNameGetRequest =
  {
    agentName: string;
    evaluatorName: string;
  };

/** @internal */
export type GetAgentEvaluatorApiV1AgentsAgentNameEvaluatorsEvaluatorNameGetRequest$Outbound =
  {
    agent_name: string;
    evaluator_name: string;
  };

/** @internal */
export const GetAgentEvaluatorApiV1AgentsAgentNameEvaluatorsEvaluatorNameGetRequest$outboundSchema:
  z.ZodMiniType<
    GetAgentEvaluatorApiV1AgentsAgentNameEvaluatorsEvaluatorNameGetRequest$Outbound,
    GetAgentEvaluatorApiV1AgentsAgentNameEvaluatorsEvaluatorNameGetRequest
  > = z.pipe(
    z.object({
      agentName: z.string(),
      evaluatorName: z.string(),
    }),
    z.transform((v) => {
      return remap$(v, {
        agentName: "agent_name",
        evaluatorName: "evaluator_name",
      });
    }),
  );

export function getAgentEvaluatorApiV1AgentsAgentNameEvaluatorsEvaluatorNameGetRequestToJSON(
  getAgentEvaluatorApiV1AgentsAgentNameEvaluatorsEvaluatorNameGetRequest:
    GetAgentEvaluatorApiV1AgentsAgentNameEvaluatorsEvaluatorNameGetRequest,
): string {
  return JSON.stringify(
    GetAgentEvaluatorApiV1AgentsAgentNameEvaluatorsEvaluatorNameGetRequest$outboundSchema
      .parse(
        getAgentEvaluatorApiV1AgentsAgentNameEvaluatorsEvaluatorNameGetRequest,
      ),
  );
}
