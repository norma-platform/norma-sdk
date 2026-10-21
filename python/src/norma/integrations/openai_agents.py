# Modified for Norma.
"""Awaited Rule guardrails for explicitly wrapped OpenAI Agents function tools.

Install ``norma-sdk[openai-agents]``. This adapter covers the framework's function
invocation path only. Direct Python calls and other tool kinds require checkpoints.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import httpx as _httpx
from pydantic import TypeAdapter

try:
    from agents import Agent as _Agent
    from agents import FunctionTool as _FunctionTool
    from agents.exceptions import (
        ToolInputGuardrailTripwireTriggered as _ToolInputGuardrailTripwireTriggered,
    )
    from agents.tool_context import ToolContext as _ToolContext
    from agents.tool_guardrails import (
        ToolGuardrailFunctionOutput as _ToolGuardrailFunctionOutput,
    )
    from agents.tool_guardrails import (
        ToolInputGuardrail as _ToolInputGuardrail,
    )
    from agents.tool_guardrails import (
        ToolInputGuardrailData as _ToolInputGuardrailData,
    )
    from agents.tool_guardrails import (
        ToolOutputGuardrail as _ToolOutputGuardrail,
    )
    from agents.tool_guardrails import (
        ToolOutputGuardrailData as _ToolOutputGuardrailData,
    )
except ImportError as exc:  # pragma: no cover - outside optional installation
    raise RuntimeError(
        "Install norma-sdk[openai-agents] to use the OpenAI Agents adapter."
    ) from exc

from norma import checks
from norma.client import NormaClient
from norma.models import Decision, JSONObject, JSONValue, Step, redact_turn_text
from norma.models.base import BaseModel
from norma.models.server import GetAgentResponse
from norma.validation import ensure_agent_name

__all__ = ["AgentBinding", "ToolCheckRecord", "protect_function_tool"]

_JSON = TypeAdapter(JSONValue)
_OBJECT = TypeAdapter(JSONObject)


@dataclass(frozen=True)
class AgentBinding:
    """Trusted mapping from one framework Agent object to an initialized Norma client.

    Call ``await binding.initialize()`` before running. A successful agent read
    verifies identity/access; every invocation still requires a hosted-v1 check.
    Reopening the client requires initialization again. No global session is used.
    """

    agent: _Agent[Any]
    client: NormaClient
    agent_name: str
    target_type: str | None = None
    target_id: str | None = None
    _initialized_client: _httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_name", ensure_agent_name(self.agent_name))
        if (self.target_type is None) != (self.target_id is None):
            raise ValueError("target_type and target_id must be provided together")

    async def initialize(self) -> None:
        """Verify the registered agent; any failure leaves this binding unusable."""
        object.__setattr__(self, "_initialized_client", None)
        opened = self.client.http_client
        if opened.is_closed:
            raise RuntimeError("Initialize AgentBinding with an open NormaClient")
        response = GetAgentResponse.model_validate(
            await self.client.check_connection(self.agent_name)
        )
        if (
            response.agent.agent_name != self.agent_name
            or self.client.http_client is not opened
            or opened.is_closed
        ):
            raise RuntimeError("Norma agent/client identity changed during initialization")
        object.__setattr__(self, "_initialized_client", opened)

    def _require_ready(self) -> None:
        if (
            self._initialized_client is None
            or self.client.http_client is not self._initialized_client
            or self._initialized_client.is_closed
        ):
            raise RuntimeError(
                "Initialize this AgentBinding with an open NormaClient before running"
            )


class ToolCheckRecord(BaseModel):
    """A correlated Rule decision retained in native guardrail output_info.

    This records a check, not proof a tool executed or that telemetry was stored.
    """

    agent_name: str | None
    framework_agent_name: str
    tool_name: str
    tool_call_id: str
    stage: Literal["pre", "post"]
    decision: Decision | None = None
    error: str | None = None
    http_status: int | None = None


@dataclass(frozen=True)
class _Proposal:
    context: _ToolContext[Any]
    agent: _Agent[Any]
    binding: AgentBinding
    canonical: str
    call_id: str
    evidence: str


def _object_pairs(pairs: list[tuple[str, JSONValue]]) -> JSONObject:
    result: JSONObject = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate tool argument keys are unsupported")
        result[key] = value
    return result


def _parse_arguments(raw: str) -> tuple[JSONObject, str]:
    parsed = _OBJECT.validate_python(json.loads(raw, object_pairs_hook=_object_pairs), strict=True)
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return parsed, canonical


def protect_function_tool(
    tool: _FunctionTool,
    *,
    bindings: Sequence[AgentBinding],
    step_name: str | None = None,
    check_output: bool = False,
    context_extractor: Callable[[_ToolContext[Any]], JSONObject] | None = None,
) -> _FunctionTool:
    """Copy a function tool with awaited prechecks and optional output checks.

    Existing schemas, guardrails, approval settings and invocation behavior remain
    on the copy. Bindings match active Agent object identity, never model-supplied
    names. Deny/error halts with a native tripwire; steer returns bounded guidance
    without invoking the rejected tool. Output checks cannot undo prior effects.

    Checks cover JSON proposals before the tool's own parsing/transformation. Use
    a checkpoint inside the function for invariants on transformed arguments.
    Built-in tools, agent-as-tool continuations and programmatic callers are not
    supported. Tool start hooks must not themselves perform the protected effect.
    """
    if type(tool) is not _FunctionTool or tool._is_agent_tool or tool._is_codex_tool:
        raise TypeError("Only ordinary FunctionTool instances are supported")
    if tool.allowed_callers:
        raise ValueError("Programmatic tool callers are outside this adapter's supported boundary")
    configured = tuple(bindings)
    if not configured or len({id(binding.agent) for binding in configured}) != len(configured):
        raise ValueError("Provide exactly one trusted binding for each supported Agent object")
    name = step_name or tool.name
    if not name.strip():
        raise ValueError("step_name must be nonempty")
    approved: ContextVar[_Proposal | None] = ContextVar(f"norma_tool_{tool.name}", default=None)
    completed: ContextVar[_Proposal | None] = ContextVar(
        f"norma_completed_{tool.name}", default=None
    )
    original_invoke = tool.on_invoke_tool

    def binding_for(agent: _Agent[Any]) -> AgentBinding:
        binding = next((item for item in configured if item.agent is agent), None)
        if binding is None:
            raise RuntimeError("Active agent has no trusted Norma binding")
        binding._require_ready()
        return binding

    async def evaluate(
        data: _ToolInputGuardrailData,
        stage: Literal["pre", "post"],
        output: Any = None,
    ) -> _ToolGuardrailFunctionOutput:
        captured_agent = data.agent
        captured_context = data.context
        captured_call_id = data.context.tool_call_id
        record = ToolCheckRecord(
            agent_name=None,
            framework_agent_name=data.agent.name,
            tool_name=tool.name,
            tool_call_id=data.context.tool_call_id,
            stage=stage,
        )
        try:
            binding = binding_for(data.agent)
            record.agent_name = binding.agent_name
            if data.context.agent is not data.agent or data.context.tool_name != tool.name:
                raise RuntimeError(
                    "Active agent/tool context does not match the guarded invocation"
                )
            if not data.context.tool_call_id:
                raise RuntimeError("Tool call correlation ID is required")
            if stage == "post":
                original = completed.get()
                if (
                    original is None
                    or original.context is not data.context
                    or original.agent is not data.agent
                    or original.binding is not binding
                    or original.call_id != data.context.tool_call_id
                ):
                    raise RuntimeError("No completed checked proposal exists for this postcheck")
                arguments, canonical = _parse_arguments(original.canonical)
                context = _OBJECT.validate_json(original.evidence, strict=True)
            else:
                arguments, canonical = _parse_arguments(data.context.tool_arguments)
                context = (
                    _OBJECT.validate_python(context_extractor(data.context), strict=True)
                    if context_extractor
                    else {}
                )
                if "norma" in context:
                    raise ValueError(
                        "The norma context key is reserved for trusted call correlation"
                    )
                context = {
                    **context,
                    "norma": {
                        "framework": "openai-agents",
                        "tool_call_id": data.context.tool_call_id,
                        "agent": data.agent.name,
                    },
                }
            evidence = json.dumps(context, allow_nan=False, sort_keys=True)
            value = _JSON.validate_python(output, strict=True) if stage == "post" else None
            decision = await checks.check(
                binding.client,
                binding.agent_name,
                Step(
                    type="tool",
                    name=name,
                    input=arguments,
                    output=value,
                    context=context,
                    tools=None,
                    ground_truth=None,
                ),
                stage,
                target_type=binding.target_type,
                target_id=binding.target_id,
            )
            binding._require_ready()
            if (
                data.agent is not captured_agent
                or data.context is not captured_context
                or captured_context.agent is not captured_agent
                or captured_context.tool_call_id != captured_call_id
                or captured_context.tool_name != tool.name
                or (
                    stage == "pre"
                    and _parse_arguments(captured_context.tool_arguments)[1] != canonical
                )
            ):
                raise RuntimeError("The tool invocation changed while its Norma check was pending")
            record.decision = decision
            failed = any(
                item.mode == "enforce" and (item.status == "error" or item.result.error is not None)
                for item in decision.checks
            )
            if failed:
                return _ToolGuardrailFunctionOutput.raise_exception(record)
            blocking = [
                item
                for item in decision.checks
                if item.mode == "enforce" and item.status in {"matched", "unresolved"}
            ]
            if any((item.effective_action or item.action) == "deny" for item in blocking):
                return _ToolGuardrailFunctionOutput.raise_exception(record)
            steering = next(
                (item for item in blocking if (item.effective_action or item.action) == "steer"),
                None,
            )
            if steering is not None:
                guidance = (
                    steering.steering_context.message
                    if steering.steering_context
                    else steering.result.message or decision.reason or "Revise the proposal"
                )
                return _ToolGuardrailFunctionOutput.reject_content(
                    redact_turn_text(guidance)[:2000], record
                )
            if not decision.allowed:
                return _ToolGuardrailFunctionOutput.raise_exception(record)
            if stage == "pre":
                approved.set(
                    _Proposal(
                        captured_context,
                        captured_agent,
                        binding,
                        canonical,
                        captured_call_id,
                        evidence,
                    )
                )
            return _ToolGuardrailFunctionOutput.allow(record)
        except Exception as exc:
            # Error types are safe diagnostics; raw responses/arguments may contain secrets.
            record.error = type(exc).__name__
            if isinstance(exc, _httpx.HTTPStatusError):
                record.http_status = exc.response.status_code
            return _ToolGuardrailFunctionOutput.raise_exception(record)

    async def before(data: _ToolInputGuardrailData) -> _ToolGuardrailFunctionOutput:
        approved.set(None)
        return await evaluate(data, "pre")

    guard: _ToolInputGuardrail[Any] = _ToolInputGuardrail(
        guardrail_function=before, name="norma_rules_pre"
    )

    async def invoke(context: _ToolContext[Any], raw: str) -> Any:
        proposal = approved.get()
        try:
            if proposal is None or proposal.context is not context:
                raise RuntimeError("No allowed Norma proposal exists for this invocation")
            proposal.binding._require_ready()
            if (
                context.agent is not proposal.agent
                or context.tool_call_id != proposal.call_id
                or context.tool_name != tool.name
                or _parse_arguments(raw)[1] != proposal.canonical
                or _parse_arguments(context.tool_arguments)[1] != proposal.canonical
            ):
                raise RuntimeError("The tool invocation changed after its Norma check")
        except Exception as exc:
            approved.set(None)
            record = ToolCheckRecord(
                agent_name=proposal.binding.agent_name if proposal else None,
                framework_agent_name=context.agent.name if context.agent else "",
                tool_name=tool.name,
                tool_call_id=context.tool_call_id,
                stage="pre",
                error=type(exc).__name__,
            )
            raise _ToolInputGuardrailTripwireTriggered(
                guardrail=guard, output=_ToolGuardrailFunctionOutput.raise_exception(record)
            ) from exc
        # A one-shot capability in this invocation task, never a global permission cache.
        approved.set(None)
        result = await original_invoke(context, raw)
        if check_output:
            completed.set(proposal)
        return result

    async def after(data: _ToolOutputGuardrailData) -> _ToolGuardrailFunctionOutput:
        try:
            return await evaluate(data, "post", data.output)
        finally:
            completed.set(None)

    outputs = list(tool.tool_output_guardrails or [])
    if check_output:
        outputs.append(_ToolOutputGuardrail(guardrail_function=after, name="norma_rules_post"))
    return replace(
        tool,
        on_invoke_tool=invoke,
        tool_input_guardrails=[*(tool.tool_input_guardrails or []), guard],
        tool_output_guardrails=outputs,
    )
