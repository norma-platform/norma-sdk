// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import { remap as remap$ } from "../../lib/primitives.js";
import * as models from "../index.js";

export type PatchAgentApiV1AgentsAgentNamePatchRequest = {
  agentName: string;
  body: models.PatchAgentRequest;
};

/** @internal */
export type PatchAgentApiV1AgentsAgentNamePatchRequest$Outbound = {
  agent_name: string;
  body: models.PatchAgentRequest$Outbound;
};

/** @internal */
export const PatchAgentApiV1AgentsAgentNamePatchRequest$outboundSchema:
  z.ZodMiniType<
    PatchAgentApiV1AgentsAgentNamePatchRequest$Outbound,
    PatchAgentApiV1AgentsAgentNamePatchRequest
  > = z.pipe(
    z.object({
      agentName: z.string(),
      body: models.PatchAgentRequest$outboundSchema,
    }),
    z.transform((v) => {
      return remap$(v, {
        agentName: "agent_name",
      });
    }),
  );

export function patchAgentApiV1AgentsAgentNamePatchRequestToJSON(
  patchAgentApiV1AgentsAgentNamePatchRequest:
    PatchAgentApiV1AgentsAgentNamePatchRequest,
): string {
  return JSON.stringify(
    PatchAgentApiV1AgentsAgentNamePatchRequest$outboundSchema.parse(
      patchAgentApiV1AgentsAgentNamePatchRequest,
    ),
  );
}
