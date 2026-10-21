// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";

export type ListAgentsApiV1AgentsGetRequest = {
  cursor?: string | null | undefined;
  limit?: number | undefined;
  name?: string | null | undefined;
};

/** @internal */
export type ListAgentsApiV1AgentsGetRequest$Outbound = {
  cursor?: string | null | undefined;
  limit: number;
  name?: string | null | undefined;
};

/** @internal */
export const ListAgentsApiV1AgentsGetRequest$outboundSchema: z.ZodMiniType<
  ListAgentsApiV1AgentsGetRequest$Outbound,
  ListAgentsApiV1AgentsGetRequest
> = z.object({
  cursor: z.optional(z.nullable(z.string())),
  limit: z._default(z.int(), 20),
  name: z.optional(z.nullable(z.string())),
});

export function listAgentsApiV1AgentsGetRequestToJSON(
  listAgentsApiV1AgentsGetRequest: ListAgentsApiV1AgentsGetRequest,
): string {
  return JSON.stringify(
    ListAgentsApiV1AgentsGetRequest$outboundSchema.parse(
      listAgentsApiV1AgentsGetRequest,
    ),
  );
}
