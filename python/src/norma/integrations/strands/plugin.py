# Modified for Norma.
"""Norma plugin integration for Strands."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Literal

import norma
from norma import RuleSteerError, RuleViolationError
from norma.integrations._tools import normalize_strands_tool_specs
from norma.models import Decision

try:
    from strands.hooks import (  # type: ignore[import-not-found]
        AfterModelCallEvent,
        AfterNodeCallEvent,
        AfterToolCallEvent,
        BeforeInvocationEvent,
        BeforeModelCallEvent,
        BeforeNodeCallEvent,
        BeforeToolCallEvent,
    )
    from strands.plugins import Plugin  # type: ignore[import-not-found]
except Exception as exc:  # pragma: no cover - optional dependency
    raise RuntimeError(
        "Strands integration requires strands-agents. Install with: norma-sdk[strands-agents]."
    ) from exc

logger = logging.getLogger(__name__)


def _action_error(result: Decision) -> tuple[str, Exception] | None:
    """Return the first blocking action as an exception."""

    matches = [
        check
        for check in result.checks
        if getattr(check, "mode", "enforce") != "observe"
        and check.status in ("matched", "unresolved")
    ]
    deny_match = next(
        (m for m in matches if (getattr(m, "effective_action", None) or m.action) == "deny"), None
    )
    if deny_match:
        msg = getattr(getattr(deny_match, "result", None), "message", None) or result.reason
        msg = msg or f"Rule '{deny_match.rule_name}' triggered"
        deny_err = RuleViolationError(
            rule_id=deny_match.rule_id,
            rule_name=deny_match.rule_name,
            message=msg,
            metadata=getattr(deny_match.result, "metadata", None),
        )
        return "deny", deny_err

    steer_match = next(
        (m for m in matches if (getattr(m, "effective_action", None) or m.action) == "steer"), None
    )
    if not steer_match:
        return None

    msg = getattr(getattr(steer_match, "result", None), "message", None) or result.reason
    msg = msg or f"Rule '{steer_match.rule_name}' triggered"
    ctx = getattr(steer_match, "steering_context", None)
    ctx_msg = getattr(ctx, "message", None) if ctx else None
    steer_err = RuleSteerError(
        rule_id=steer_match.rule_id,
        rule_name=steer_match.rule_name,
        message=f"Steering required [{steer_match.rule_name}]: {msg}",
        metadata=getattr(steer_match.result, "metadata", None),
        steering_context=ctx_msg or msg,
    )
    return "steer", steer_err


class NormaPlugin(Plugin):
    """Plugin that integrates Norma with Strands lifecycle events.

    The Norma server is required for rule distribution and policy assignment.
    Rules are evaluated by the hosted runtime.
    Strands lifecycle events handled by this adapter are model and tool events,
    so its internal step-type annotations intentionally remain limited to
    ``llm`` and ``tool``. Custom types remain available through the core SDK APIs.
    """

    name = "norma-plugin"

    def __init__(
        self,
        agent_name: str,
        event_types: list[type] | None = None,
        on_violation_callback: Callable[[dict[str, Any], Decision], None] | None = None,
        enable_logging: bool = True,
    ) -> None:
        super().__init__()
        self.agent_name = agent_name
        self.event_types = event_types
        self.on_violation_callback = on_violation_callback
        self.enable_logging = enable_logging
        self._tool_registry: Any | None = None

    def _invoke_callback(self, rule_name: str, stage: str, result: Decision) -> None:
        if self.on_violation_callback:
            self.on_violation_callback(
                {
                    "agent": self.agent_name,
                    "rule_name": rule_name,
                    "stage": stage,
                },
                result,
            )

    def _raise_error(self, error: Exception, use_runtime_error: bool) -> None:
        if use_runtime_error:
            raise RuntimeError(str(error))
        raise error

    async def _evaluate_and_enforce(
        self,
        step_name: str,
        input: Any | None = None,
        output: Any | None = None,
        context: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        step_type: Literal["tool", "llm"] = "llm",
        stage: Literal["pre", "post"] = "pre",
        use_runtime_error: bool = False,
    ) -> None:
        result = await norma.evaluate_rules(
            step_name=step_name,
            input=input,
            output=output,
            context=context,
            tools=tools,
            step_type=step_type,
            stage=stage,
            agent_name=self.agent_name,
        )

        if [
            check
            for check in result.checks
            if getattr(check, "mode", "enforce") != "observe" and check.status == "error"
        ]:
            error_names = ", ".join(
                e.rule_name
                for e in [
                    check
                    for check in result.checks
                    if getattr(check, "mode", "enforce") != "observe" and check.status == "error"
                ]
                if getattr(e, "rule_name", None)
            )
            self._raise_error(
                RuntimeError(
                    "Rule check failed; execution blocked for safety. "
                    f"Errors: {error_names or 'unknown'}"
                ),
                use_runtime_error,
            )

        action = _action_error(result)
        if action:
            _, err = action
            rule_name = getattr(err, "rule_name", "unknown")
            self._invoke_callback(rule_name, stage, result)
            if isinstance(err, RuleSteerError):
                logger.debug(
                    "agent=<%s>, step=<%s>, stage=<%s> | steering required",
                    self.agent_name,
                    step_name,
                    stage,
                )
            self._raise_error(err, use_runtime_error)

        if not result.allowed:
            rule_name = "unknown"
            reason = result.reason

            if [
                check
                for check in result.checks
                if getattr(check, "mode", "enforce") != "observe"
                and check.status in ("matched", "unresolved")
            ]:
                first_match = [
                    check
                    for check in result.checks
                    if getattr(check, "mode", "enforce") != "observe"
                    and check.status in ("matched", "unresolved")
                ][0]
                rule_name = first_match.rule_name
                if not reason:
                    match_result = getattr(first_match, "result", None)
                    msg = getattr(match_result, "message", None) if match_result else None
                    reason = msg or f"Rule '{rule_name}' triggered"

            logger.debug(
                "agent=<%s>, rule=<%s> | rule violation",
                self.agent_name,
                rule_name,
            )
            self._invoke_callback(rule_name, stage, result)
            self._raise_error(
                RuleViolationError(
                    rule_name=rule_name,
                    message=reason or "Rule violation",
                    metadata=(
                        getattr(first_match.result, "metadata", None)
                        if [
                            check
                            for check in result.checks
                            if getattr(check, "mode", "enforce") != "observe"
                            and check.status in ("matched", "unresolved")
                        ]
                        else None
                    ),
                ),
                use_runtime_error,
            )

    def init_agent(self, agent: Any) -> None:
        self._tool_registry = getattr(agent, "tool_registry", None)
        event_map = {
            BeforeInvocationEvent: self.check_before_invocation,
            BeforeModelCallEvent: self.check_before_model,
            AfterModelCallEvent: self.check_after_model,
            BeforeToolCallEvent: self.check_before_tool,
            AfterToolCallEvent: self.check_after_tool,
            BeforeNodeCallEvent: self.check_before_node,
            AfterNodeCallEvent: self.check_after_node,
        }

        events_to_register = self.event_types if self.event_types else event_map.keys()

        if self.enable_logging:
            logger.debug(
                "agent=<%s>, events=<%s> | registering hooks",
                self.agent_name,
                ",".join([e.__name__ for e in events_to_register if e in event_map]),
            )

        for event_type in events_to_register:
            if event_type in event_map:
                agent.add_hook(event_map[event_type], event_type)

    async def check_before_invocation(self, event: BeforeInvocationEvent) -> None:
        input_text, _ = self._extract_messages(event)
        await self._evaluate_and_enforce(
            step_name="check_before_invocation",
            input=input_text,
            tools=self._available_tools(),
            step_type="llm",
            stage="pre",
        )

    async def check_before_model(self, event: BeforeModelCallEvent) -> None:
        input_text, _ = self._extract_messages(event)
        await self._evaluate_and_enforce(
            step_name="check_before_model",
            input=input_text,
            tools=self._available_tools(),
            step_type="llm",
            stage="pre",
        )

    async def check_after_model(self, event: AfterModelCallEvent) -> None:
        input_text, output_text = self._extract_messages(event)
        context = self._extract_context(event)
        await self._evaluate_and_enforce(
            step_name="check_after_model",
            input=input_text,
            output=output_text,
            context=context,
            tools=self._available_tools(),
            step_type="llm",
            stage="post",
        )

    async def check_before_tool(self, event: BeforeToolCallEvent) -> None:
        tool_name, tool_input = self._extract_tool_data(event)
        context = self._extract_context(event)
        await self._evaluate_and_enforce(
            step_name=tool_name,
            input=tool_input,
            context=context,
            step_type="tool",
            stage="pre",
            use_runtime_error=True,
        )

    async def check_after_tool(self, event: AfterToolCallEvent) -> None:
        tool_name, tool_output = self._extract_tool_data(event)
        tool_input: Any = event.tool_use.get("input", {}) if event.tool_use else {}
        context = self._extract_context(event)
        await self._evaluate_and_enforce(
            step_name=tool_name,
            input=tool_input,
            output=tool_output,
            context=context,
            step_type="tool",
            stage="post",
            use_runtime_error=True,
        )

    async def check_before_node(self, event: BeforeNodeCallEvent) -> None:
        input_text, _ = self._extract_messages(event)
        node_id = getattr(event, "node_id", "unknown")
        context = self._extract_context(event)
        await self._evaluate_and_enforce(
            step_name=node_id,
            input=input_text,
            context=context,
            tools=self._available_tools(),
            step_type="llm",
            stage="pre",
        )

    async def check_after_node(self, event: AfterNodeCallEvent) -> None:
        input_text, output_text = self._extract_messages(event)
        node_id = getattr(event, "node_id", "unknown")
        context = self._extract_context(event)
        await self._evaluate_and_enforce(
            step_name=node_id,
            input=input_text,
            output=output_text,
            context=context,
            tools=self._available_tools(),
            step_type="llm",
            stage="post",
        )

    def _available_tools(self) -> list[dict[str, Any]] | None:
        """Return the complete current Strands registry in normalized form."""
        get_specs = getattr(self._tool_registry, "get_all_tool_specs", None)
        if not callable(get_specs):
            return None
        try:
            return normalize_strands_tool_specs(get_specs())
        except Exception:
            logger.warning("Unable to capture complete Strands tool definitions", exc_info=True)
            return None

    def _extract_user_message_from_list(self, messages: list | None, reverse: bool = False) -> str:
        if not messages:
            return ""
        msg_iter = reversed(messages) if reverse else messages
        for msg in msg_iter:
            if isinstance(msg, dict) and msg.get("role") == "user":
                return self._extract_content_text(msg.get("content", ""))
        return ""

    def _extract_messages(self, event: Any) -> tuple[str, str]:
        input_text = ""
        output_text = ""

        if isinstance(event, BeforeInvocationEvent):
            input_text = self._extract_user_message_from_list(event.messages)
        elif isinstance(event, BeforeModelCallEvent):
            state = event.invocation_state or {}
            if "messages" in state:
                input_text = self._extract_user_message_from_list(state["messages"], reverse=True)
            elif "input" in state:
                input_text = self._extract_content_text(state["input"])
        elif isinstance(event, AfterModelCallEvent):
            state = event.invocation_state or {}
            if "messages" in state:
                input_text = self._extract_user_message_from_list(state["messages"], reverse=True)
            elif "input" in state:
                input_text = self._extract_content_text(state["input"])
            if event.stop_response:
                message_content = event.stop_response.message.get("content", [])
                output_text = self._extract_content_text(message_content)
        elif isinstance(event, BeforeNodeCallEvent):
            state = event.invocation_state or {}
            if "messages" in state:
                input_text = self._extract_user_message_from_list(state["messages"], reverse=True)
            elif "input" in state:
                input_text = self._extract_content_text(state["input"])
        elif isinstance(event, AfterNodeCallEvent):
            state = event.invocation_state or {}
            for key in ("output", "result", "response", "messages"):
                if key in state:
                    output_text = self._extract_content_text(state[key])
                    break
            if "messages" in state:
                input_text = self._extract_user_message_from_list(state["messages"], reverse=True)
            elif "input" in state:
                input_text = self._extract_content_text(state["input"])

        return input_text, output_text

    def _extract_context(self, event: Any) -> dict[str, Any] | None:
        state = getattr(event, "invocation_state", None)
        if isinstance(state, dict):
            ctx = state.get("context")
            if isinstance(ctx, dict):
                return ctx
        return None

    def _extract_tool_data(
        self,
        event: BeforeToolCallEvent | AfterToolCallEvent,
    ) -> tuple[str, Any]:
        if event.selected_tool:
            tool_name = event.selected_tool.tool_name
        else:
            tool_name = event.tool_use.get("name", "unknown-tool")

        tool_data: Any
        if isinstance(event, BeforeToolCallEvent):
            tool_data = event.tool_use.get("input", {})
        else:
            if event.exception:
                tool_data = f"ERROR: {str(event.exception)}"
            else:
                tool_data = self._extract_content_text(event.result.get("content", []))

        return tool_name, tool_data

    def _extract_content_text(self, content: Any) -> str:
        if not content:
            return ""

        if isinstance(content, str):
            return content

        if isinstance(content, list):
            text_parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    if "text" in block:
                        text_parts.append(block["text"])
                    elif "content" in block:
                        nested = self._extract_content_text(block["content"])
                        if nested:
                            text_parts.append(nested)
                    elif "citationsContent" in block:
                        citations_block = block["citationsContent"]
                        if "content" in citations_block:
                            for citation_item in citations_block["content"]:
                                if isinstance(citation_item, dict) and "text" in citation_item:
                                    text_parts.append(citation_item["text"])
                    elif "toolUse" in block:
                        tool_name = block["toolUse"].get("name", "unknown")
                        text_parts.append(f"[tool_use: {tool_name}]")
                    elif "toolResult" in block:
                        result_content = block["toolResult"].get("content", [])
                        result_text = self._extract_content_text(result_content)
                        if result_text:
                            text_parts.append(result_text)
                else:
                    text_parts.append(str(block))
            return "\n".join(text_parts)

        if isinstance(content, dict):
            if "text" in content:
                return str(content["text"])
            if "content" in content:
                return self._extract_content_text(content["content"])
            if "json" in content:
                import json

                return json.dumps(content["json"])

        return str(content)
