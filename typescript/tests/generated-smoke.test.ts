
import { describe, expect, it } from "vitest";

import { stepToJSON } from "../src/generated/models/step";
import { NormaSDK } from "../src/generated/sdk/sdk";
import { SDK_METADATA } from "../src/generated/lib/config";
import { NormaSDKCore } from "../src/generated/core";
import { NormaSDKError } from "../src/generated/models/errors/norma-sdk-error";
import { NormaSDKDefaultError } from "../src/generated/models/errors/norma-sdk-default-error";
import packageMetadata from "../package.json";

describe("generated client layout", () => {
  it("retains Norma identity in generated constructors, errors and client metadata", () => {
    // Given: locally generated/normalized infrastructure with no live transport.
    const request = new Request("https://qualification.invalid/api/v1/agents");
    const metadata = { request, response: new Response("error", { status: 500 }), body: "error" };
    // When: creating the concrete generated error classes.
    const base = new NormaSDKError("failed", metadata);
    const fallback = new NormaSDKDefaultError("failed", metadata);
    // Then: SDK identity changes without changing the response contract or upstream attribution.
    expect(NormaSDK.name).toBe("NormaSDK");
    expect(NormaSDKCore.name).toBe("NormaSDKCore");
    expect(base.name).toBe("NormaSDKError");
    expect(fallback.name).toBe("NormaSDKDefaultError");
    expect(fallback).toBeInstanceOf(NormaSDKError);
    expect(fallback.statusCode).toBe(500);
    expect(JSON.parse(fallback.body)).toMatchObject({ code: "NORMA_RUNTIME_UNAVAILABLE", status: 500 });
    expect(SDK_METADATA.sdkVersion).toBe(packageMetadata.version);
    expect(SDK_METADATA.userAgent).toContain(`typescript ${packageMetadata.version} `);
    expect(SDK_METADATA.userAgent).toMatch(/^speakeasy-sdk\/typescript .+ norma-sdk$/);
  });



  it("serializes structured Step scorer context", () => {
    const serialized = stepToJSON({
      type: "llm",
      name: "answer",
      input: "question",
      output: "answer",
      groundTruth: "expected",
      tools: [
        {
          name: "search",
          description: "Search documents",
          input_schema: { type: "object" },
        },
      ],
    });

    expect(JSON.parse(serialized)).toEqual({
      type: "llm",
      name: "answer",
      input: "question",
      output: "answer",
      ground_truth: "expected",
      tools: [
        {
          name: "search",
          description: "Search documents",
          input_schema: { type: "object" },
        },
      ],
    });
  });

});
