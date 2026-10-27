# Console control authoring helpers

The Python and TypeScript SDKs export pure helpers for inspecting authored Console rule controls before sending a rule to Norma. The server remains authoritative: the helper never prepares or publishes a rule, resolves an agent override, or decides whether a request is safe to save.

Both languages expose `validate_authored_controls` / `validateAuthoredControls`. Pass raw JSON text when possible so the helper can reject duplicate keys, invalid Unicode, non-finite numbers, and oversized or overly deep sources. Parsed-object input is copied and checked as plain JSON data, but reports `source_validation="unavailable"` because duplicate keys and original number spellings are already lost.

Pass the trusted `rule-controls-v1` capability projection explicitly. A capability is never inferred from fields authored in the rule. A known protected destination or invalid declaration returns `status="invalid"`; unavailable manifests, unknown structural operators, uncertain parsed-source admission, or destinations requiring full validation return `status="requires_server_validation"`. Only a fully checked local result returns `status="valid"`. Every control declaration needs a valid default, and control references require `execution="server"`.

The result carries bounded diagnostics with RFC 6901 source pointers, a recursively immutable authored-document copy, and typed reference pointers. Capability slots expose expected-value constraints, but complete evaluator configuration, cross-field, provider, regex, and runtime checks still belong to server preparation. A local `valid` result is an authoring aid, not a server preparation receipt or a release/impact assessment.

The helper enforces the shared control limits: at most 32 declarations, 256 reference sites, 50 returned diagnostics, 4,096 Unicode scalars per string value, 128 list items, a 1 MiB raw-source limit, and a 16 KiB compact UTF-8 JSON limit for the complete effective values map. TypeScript returns `requires_server_validation` instead of claiming an exact byte-bound result when floating-point number spelling cannot be proven equivalent to the server serializer.

```python
from norma import validate_authored_controls

result = validate_authored_controls(rule_json_text, trusted_capabilities)
if result.status == "invalid":
    for diagnostic in result.diagnostics:
        print(diagnostic.code, diagnostic.source_pointer)
```

```ts
import { validateAuthoredControls } from "norma-sdk";

const result = validateAuthoredControls(ruleJsonText, trustedCapabilities);
if (result.status === "invalid") {
  for (const diagnostic of result.diagnostics) {
    console.log(diagnostic.code, diagnostic.sourcePointer);
  }
}
```

## Author a rule and inspect its frozen runtime values

Create and publish the rule in Console. The declaration owns the shared default; a Console administrator can then set a sparse per-agent override. For example, this rule uses a shared minimum of 90 days and lets the agent `support-agent` use 180:

```json
{
  "execution": "server",
  "enabled": true,
  "action": { "decision": "deny" },
  "scope": { "step_types": ["llm"], "stages": ["pre"] },
  "selector": { "path": "input" },
  "evaluator": {
    "name": "json",
    "config": {
      "required_fields": ["age_days"],
      "field_types": { "age_days": "number" },
      "field_constraints": {
        "age_days": { "min": { "$param": "minimum_age_days" } }
      }
    }
  },
  "controls": {
    "minimum_age_days": {
      "type": "number",
      "label": "Minimum age in days",
      "default": 90,
      "minimum": 0,
      "maximum": 365
    }
  }
}
```

Set the agent override in Console's **Configure controls** editor. The client opts into `rule-controls-v1` to require both the response header and response envelope before accepting control receipts. Opt-in does not create or update control values; control management remains in Console. Omitting the option preserves the legacy client projection.

Before running the example, configure `NORMA_RUNTIME_URL` and `NORMA_AGENT_KEY` for the runtime and the scoped `support-agent` key. The rule must be published and attached to that agent, and the 180-day override must already exist in Console. SDK startup and checks do not create or change either resource.

Python can use the explicit check API to inspect a decision and its immutable receipt/evidence:

```python
import asyncio

import norma


async def main() -> None:
    async with norma.NormaClient(control_contract="rule-controls-v1") as client:
        decision = await norma.checks.check(
            client,
            "support-agent",
            norma.Step(type="llm", name="verify_age", input={"age_days": 120}),
            "pre",
        )
        assert decision.allowed is False
        if not decision.control_resolutions or not decision.control_evidence:
            raise RuntimeError("The runtime did not return opted-in control proof.")
        receipt = decision.control_resolutions[0]
        assert receipt.defaults["minimum_age_days"] == 90
        assert receipt.effective_values["minimum_age_days"] == 180
        assert receipt.value_sources["minimum_age_days"] == "agent_override"
        evidence = decision.control_evidence[0]
        age = evidence.references[0]
        comparison = age.numeric_comparisons[0]
        assert age.disposition == "evaluated"
        assert (comparison.observed, comparison.expected, comparison.outcome) == (120, 180, "failed")


asyncio.run(main())
```

The evidence row is the recorded result for that check, not a live lookup. Its numeric comparison reports the observed 120 against the frozen expected minimum 180 and the actual pass/fail outcome. Redacted observations remain redacted; incomplete or unavailable evidence must not be filled from current Console settings.

TypeScript uses the configured `NormaClient` and its existing check API:

```ts
import { NormaClient } from "norma-sdk";

const client = new NormaClient();
client.init({ agentName: "support-agent", controlContract: "rule-controls-v1" });

const decision = await client.checks.check({
  stage: "pre",
  step: { type: "llm", name: "verify_age", input: { age_days: 120 } },
});
const receipt = decision.controlResolutions?.[0];
if (!receipt || !decision.controlEvidence?.[0]) {
  throw new Error("The runtime did not return opted-in control proof.");
}
if (decision.allowed) throw new Error("The configured minimum should deny age 120.");
console.log(receipt.defaults.minimum_age_days); // 90
console.log(receipt.effectiveValues.minimum_age_days); // 180
console.log(receipt.valueSources.minimum_age_days); // "agent_override"
const age = decision.controlEvidence[0].references[0];
if (!age) throw new Error("The runtime did not return the expected age reference.");
const comparison = age.numericComparisons[0];
if (age.disposition !== "evaluated" || comparison?.observed !== 120 ||
    comparison.expected !== 180 || comparison.outcome !== "failed") {
  throw new Error("The returned evidence does not describe the expected comparison.");
}
```

Registration, agent-rule reads, and checks use the same explicit opt-in when configured. The SDK rejects a missing or mismatched acknowledgement and does not retry a check or repeat an external action. The existing hosted execution proof remains required independently of the controls acknowledgement.
