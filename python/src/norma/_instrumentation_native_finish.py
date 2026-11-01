# Modified for Norma.
"""Compile and install the supported native finish adapters for v2 configs."""

from __future__ import annotations

import inspect
import types
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from typing import Any, cast

from norma.models import StepSchema

from ._instrumentation_values import compile_value_source, resolve_binding
from .checkpoints import CheckError

_RUN_DISPATCHER_MARKER = "__norma_native_finish_runner_dispatcher__"


@dataclass(frozen=True)
class _ToolSpec:
    name: str
    tool: object
    check_output: bool
    context_extractor: object | None
    schema: StepSchema
    action_resolver: (
        Callable[[dict[str, object]], Mapping[str, object] | Awaitable[Mapping[str, object]]] | None
    )


@dataclass(frozen=True)
class NativeAdapterDescriptor:
    """Static, network-free description of one configured native adapter."""

    agent_name: str
    adapter_name: str
    kind: str
    native_agent: object
    runner: type[object] | None = None
    tools: tuple[_ToolSpec, ...] = ()
    on_verdict: object | None = None
    schemas: tuple[StepSchema, ...] = ()
    finish_enabled: bool = False


def _compile_action_resolver(
    action: object, bindings: Mapping[str, object]
) -> Callable[[dict[str, object]], Awaitable[Mapping[str, object]]] | None:
    if action is None:
        return None
    if not isinstance(action, Mapping):
        raise ValueError("OpenAI tool action must be an action object")
    if "cwd" in action:
        raise ValueError(
            "OpenAI native tool actions do not retain cwd in TurnRecord evidence"
        )
    kind = action.get("kind")
    readers: dict[str, Callable[[dict[str, object]], object]] = {}
    for field_name in ("target", "command"):
        source = action.get(field_name)
        if source is None or isinstance(source, str):
            def literal_reader(
                _invocation: dict[str, object], value: object = source
            ) -> object:
                return value

            readers[field_name] = literal_reader
        elif isinstance(source, Mapping):
            readers[field_name] = compile_value_source(
                source,
                bindings,
                precheck=True,
                native_input=True,
            )
        else:
            raise ValueError(
                f"OpenAI tool action {field_name} must be a string, null, or value source"
            )

    async def resolve(invocation: dict[str, object]) -> Mapping[str, object]:
        values: dict[str, object] = {"kind": kind}
        for field_name, reader in readers.items():
            value = reader(invocation)
            if inspect.isawaitable(value):
                value = await value
            if value is not None and not isinstance(value, str):
                raise ValueError(
                    f"OpenAI tool action {field_name} must resolve to a string or null"
                )
            values[field_name] = value
        return values

    return resolve


def compile_native_adapters(
    config: dict[str, Any], bindings: dict[str, object] | Any, agent_name: str
) -> list[NativeAdapterDescriptor]:
    """Resolve configured OpenAI/Pydantic objects without HTTP or mutation."""
    adapters = config.get("adapters", {})
    if not isinstance(adapters, dict):
        return []
    compiled: list[NativeAdapterDescriptor] = []
    adapter_kinds: dict[str, str] = {}
    claimed_agents: dict[int, tuple[object, str]] = {}
    claimed_tool_lists: dict[int, tuple[list[object], object, str]] = {}
    for adapter_name, adapter in adapters.items():
        kind = adapter.get("kind")
        if kind not in {"openai_agents", "pydantic_ai"}:
            adapter_kinds[adapter_name] = kind
            continue
        try:
            native_agent = resolve_binding(adapter["agent"]["binding"], bindings)
        except (KeyError, TypeError) as exc:
            raise ValueError(f"Adapter '{adapter_name}' has an invalid agent binding") from exc
        previous = claimed_agents.get(id(native_agent))
        if previous is not None and previous[0] is native_agent:
            raise ValueError(
                f"Native Agent is already assigned to adapter '{previous[1]}' "
                f"for agent '{agent_name}'"
            )
        claimed_agents[id(native_agent)] = (native_agent, adapter_name)
        if kind == "openai_agents":
            descriptor = _compile_openai_adapter(
                adapter_name, agent_name, adapter, native_agent, bindings
            )
            native_tools = cast(list[object], getattr(native_agent, "tools"))
            previous_list = claimed_tool_lists.get(id(native_tools))
            if previous_list is not None and previous_list[0] is native_tools:
                raise ValueError(
                    f"OpenAI Agent tools list is already assigned to adapter "
                    f"'{previous_list[2]}' for agent '{agent_name}'"
                )
            claimed_tool_lists[id(native_tools)] = (native_tools, native_agent, adapter_name)
        else:
            descriptor = _compile_pydantic_adapter(adapter_name, agent_name, adapter, native_agent)
        compiled.append(descriptor)
        adapter_kinds[adapter_name] = kind

    for entrypoint in config.get("entrypoints", []):
        if not isinstance(entrypoint, dict):
            continue
        finish = (entrypoint.get("turn") or {}).get("finish")
        if not isinstance(finish, dict) or finish.get("driver") not in {
            "openai_agents",
            "pydantic_ai",
        }:
            continue
        adapter_name = cast(str, finish.get("adapter"))
        expected_kind = finish["driver"]
        if adapter_kinds.get(adapter_name) != expected_kind:
            raise ValueError(
                f"Turn finish adapter '{adapter_name}' must reference a configured "
                f"'{expected_kind}' adapter"
            )
        descriptor_index = next(
            index
            for index, descriptor in enumerate(compiled)
            if descriptor.adapter_name == adapter_name and descriptor.kind == expected_kind
        )
        selected = compiled[descriptor_index]
        if expected_kind == "openai_agents" and selected.runner is None:
            raise ValueError(f"OpenAI finish adapter '{adapter_name}' requires a bound Runner")
        compiled[descriptor_index] = dataclass_replace(selected, finish_enabled=True)

    return compiled


def _compile_openai_adapter(
    adapter_name: str,
    agent_name: str,
    adapter: dict[str, Any],
    native_agent: object,
    bindings: dict[str, object] | Any,
) -> NativeAdapterDescriptor:
    try:
        from agents import Agent, FunctionTool, Runner
    except ImportError as exc:
        raise RuntimeError(
            "The configured OpenAI Agents adapter requires norma-sdk[openai-agents]"
        ) from exc
    if not isinstance(native_agent, Agent):
        raise ValueError(f"Adapter '{adapter_name}' agent binding must resolve to an OpenAI Agent")
    native_tools = getattr(native_agent, "tools", None)
    if type(native_tools) is not list:
        raise ValueError(
            f"Adapter '{adapter_name}' requires an OpenAI Agent with a mutable tools list"
        )
    runner: type[object] | None = None
    if "runner" in adapter:
        try:
            resolved_runner = resolve_binding(adapter["runner"]["binding"], bindings)
        except (KeyError, TypeError) as exc:
            raise ValueError(f"Adapter '{adapter_name}' has an invalid Runner binding") from exc
        if not isinstance(resolved_runner, type) or not issubclass(resolved_runner, Runner):
            raise ValueError(f"Adapter '{adapter_name}' runner binding must resolve to Runner")
        runner = cast(type[object], resolved_runner)
        descriptor = vars(runner).get("run")
        if not isinstance(descriptor, classmethod):
            raise ValueError(
                f"Adapter '{adapter_name}' Runner.run must be an owned classmethod slot"
            )
        if getattr(descriptor.__func__, _RUN_DISPATCHER_MARKER, False):
            raise ValueError("Runner.run is already owned by a configured Norma adapter")

    tools: list[_ToolSpec] = []
    claimed_tool_ids: set[int] = set()
    for name, tool_config in (adapter.get("tools") or {}).items():
        tool_binding = tool_config.get("tool")
        if not isinstance(tool_binding, dict) or not isinstance(tool_binding.get("binding"), str):
            raise ValueError(f"OpenAI adapter tool '{name}' requires a native tool binding")
        tool = resolve_binding(tool_binding["binding"], bindings)
        if type(tool) is not FunctionTool or tool._is_agent_tool or tool._is_codex_tool:
            raise ValueError(f"OpenAI adapter tool '{name}' must be an ordinary FunctionTool")
        if tool.allowed_callers:
            raise ValueError(f"OpenAI adapter tool '{name}' cannot restrict programmatic callers")
        if not any(candidate is tool for candidate in native_tools):
            raise ValueError(f"OpenAI adapter tool '{name}' must be present on its bound Agent")
        if id(tool) in claimed_tool_ids:
            raise ValueError(
                "One OpenAI FunctionTool cannot be assigned to multiple configured steps"
            )
        claimed_tool_ids.add(id(tool))
        context_extractor = None
        if "context_extractor" in tool_config:
            context_extractor = resolve_binding(
                tool_config["context_extractor"]["binding"], bindings
            )
            if not callable(context_extractor):
                raise ValueError(f"OpenAI tool '{name}' context_extractor must be a callback")
        schema_data: dict[str, Any] = {
            "type": "tool",
            "name": name,
            "input_schema": tool.params_json_schema,
        }
        if tool.description:
            schema_data["description"] = tool.description
        if tool.output_json_schema is not None:
            schema_data["output_schema"] = tool.output_json_schema
        action_resolver = _compile_action_resolver(tool_config.get("action"), bindings)
        if action_resolver is not None:
            from agents.tool import _FailureHandlingFunctionToolInvoker

            if not isinstance(tool.on_invoke_tool, _FailureHandlingFunctionToolInvoker):
                raise ValueError(
                    f"OpenAI tool '{name}' action recording requires the framework's "
                    "failure-handling FunctionTool invoker"
                )
        tools.append(
            _ToolSpec(
                name,
                tool,
                bool(tool_config.get("check_output", False)),
                context_extractor,
                StepSchema.model_validate(schema_data),
                action_resolver,
            )
        )
    return NativeAdapterDescriptor(
        agent_name,
        adapter_name,
        "openai_agents",
        native_agent,
        runner,
        tuple(tools),
        schemas=tuple(tool.schema for tool in tools),
    )


def _compile_pydantic_adapter(
    adapter_name: str, agent_name: str, adapter: dict[str, Any], native_agent: object
) -> NativeAdapterDescriptor:
    try:
        from pydantic_ai import Agent
    except ImportError as exc:
        raise RuntimeError(
            "The configured Pydantic AI adapter requires norma-sdk[pydantic-ai]"
        ) from exc
    if not isinstance(native_agent, Agent):
        raise ValueError(
            f"Adapter '{adapter_name}' agent binding must resolve to a Pydantic AI Agent"
        )
    if getattr(native_agent, "_output_type", None) is not str:
        raise ValueError(f"Pydantic finish adapter '{adapter_name}' requires a text-output Agent")
    if "run" in vars(native_agent):
        raise ValueError(f"Pydantic Agent '{adapter_name}' already owns an instance run slot")
    return NativeAdapterDescriptor(agent_name, adapter_name, "pydantic_ai", native_agent)


@dataclass
class _ToolSlot:
    agent: object
    tool_list: list[object]
    original: object
    replacement: object

    def install(self) -> None:
        self.validate()
        index = next(i for i, value in enumerate(self.tool_list) if value is self.original)
        self.tool_list[index] = self.replacement

    def validate(self) -> None:
        if getattr(self.agent, "tools", None) is not self.tool_list:
            raise ValueError("OpenAI Agent tools list changed during configuration")
        if not any(value is self.original for value in self.tool_list):
            raise ValueError("OpenAI FunctionTool changed during configuration")

    def restore(self) -> None:
        if getattr(self.agent, "tools", None) is not self.tool_list:
            return
        index = next(
            (i for i, value in enumerate(self.tool_list) if value is self.replacement), None
        )
        if index is not None:
            self.tool_list[index] = self.original


class PreparedNativeAdapters:
    """One transactional install batch for configured OpenAI/Pydantic adapters."""

    def __init__(self, descriptors: list[NativeAdapterDescriptor], sessions: dict[str, object]):
        self.descriptors = descriptors
        self.sessions = sessions
        self.schemas: dict[str, list[StepSchema]] = {}
        self._tool_slots: list[_ToolSlot] = []
        self._runner_slots: list[tuple[type[object], object, object]] = []
        self._pydantic_slots: list[tuple[Any, Any, Any, Any, bool]] = []
        self._installed = False
        self._closed = False
        self._prepare()

    def _prepare(self) -> None:
        for descriptor in self.descriptors:
            session = self.sessions.get(descriptor.agent_name)
            if session is None:
                raise ValueError(f"No configured session for Agent '{descriptor.agent_name}'")
            self.schemas.setdefault(descriptor.agent_name, []).extend(descriptor.schemas)
            if descriptor.kind == "openai_agents":
                from .integrations.openai_agents import AgentBinding, protect_function_tool

                binding = AgentBinding(
                    agent=cast(Any, descriptor.native_agent),
                    client=None,
                    agent_name=descriptor.agent_name,
                    target_type=getattr(session, "target_type", None),
                    target_id=getattr(session, "target_id", None),
                    _session=session,
                )
                tool_list = cast(list[object], getattr(descriptor.native_agent, "tools"))
                for tool_spec in descriptor.tools:
                    protected = protect_function_tool(
                        cast(Any, tool_spec.tool),
                        bindings=[binding],
                        step_name=tool_spec.name,
                        check_output=tool_spec.check_output,
                        context_extractor=cast(Any, tool_spec.context_extractor),
                        action_resolver=tool_spec.action_resolver,
                    )
                    self._tool_slots.append(
                        _ToolSlot(descriptor.native_agent, tool_list, tool_spec.tool, protected)
                    )
            elif descriptor.kind == "pydantic_ai":
                from .integrations.pydantic_ai_finish import PydanticFinishGuard

                if not descriptor.finish_enabled:
                    continue
                guard = PydanticFinishGuard(cast(Any, descriptor.native_agent), _defer_install=True)
                original_run = getattr(descriptor.native_agent, "run")
                self._pydantic_slots.append(
                    (descriptor.native_agent, original_run, guard, None, False)
                )
        self._prepare_runner_slots()

    def _prepare_runner_slots(self) -> None:
        by_runner: dict[type[object], list[NativeAdapterDescriptor]] = {}
        for descriptor in self.descriptors:
            if (
                descriptor.kind == "openai_agents"
                and descriptor.finish_enabled
                and descriptor.runner is not None
            ):
                by_runner.setdefault(descriptor.runner, []).append(descriptor)
        for runner, owners in by_runner.items():
            original_descriptor = vars(runner).get("run")
            if not isinstance(original_descriptor, classmethod):
                raise ValueError("Configured Runner.run slot changed during preparation")
            original_function = original_descriptor.__func__
            if getattr(original_function, _RUN_DISPATCHER_MARKER, False):
                raise ValueError("Runner.run is already owned by another Norma configuration")
            dispatcher = self._make_runner_dispatcher(runner, original_descriptor, owners)
            setattr(dispatcher, _RUN_DISPATCHER_MARKER, True)
            self._runner_slots.append((runner, original_descriptor, classmethod(dispatcher)))

    def _make_runner_dispatcher(
        self,
        runner: type[object],
        original_descriptor: classmethod,
        owners: list[NativeAdapterDescriptor],
    ) -> Any:
        original_function = original_descriptor.__func__
        from .integrations.openai_agents_finish import _original_agent_for_finish_clone

        async def dispatch(
            cls: type[object], starting_agent: object, input: object, **kwargs: Any
        ) -> Any:
            original = original_function.__get__(None, cls)
            from .finish import current_configured_native_finish, mark_configured_native_finish_used

            record = current_configured_native_finish()
            if record is None or record.driver != "openai_agents":
                return await original(starting_agent, input, **kwargs)
            record.ensure_active()
            selected_agent = _original_agent_for_finish_clone(cast(Any, starting_agent))
            selected = next(
                (
                    descriptor
                    for descriptor in owners
                    if descriptor.agent_name == record.agent_name
                    and descriptor.adapter_name == record.adapter_name
                    and descriptor.native_agent is selected_agent
                ),
                None,
            )
            if selected is None:
                raise CheckError(
                    "Configured OpenAI finish called Runner with an Agent that is not bound "
                    "to the selected adapter"
                )
            if not isinstance(input, (str, list)):
                raise TypeError(
                    "Configured OpenAI finish supports text or message-list Runner input"
                )
            mark_configured_native_finish_used(record)
            from .client import NormaClient, new_session_client
            from .integrations.openai_agents_finish import run_openai_agents_with_finish

            async with new_session_client(NormaClient, record.session) as client:
                finished = await run_openai_agents_with_finish(
                    cast(Any, selected.native_agent),
                    cast(Any, input),
                    record.recorder,
                    client=client,
                    agent_name=record.agent_name,
                    target_type=record.target_type,
                    target_id=record.target_id,
                    context=kwargs.get("context"),
                    session=kwargs.get("session"),
                    runner_options=kwargs,
                    ensure_active=record.ensure_active,
                )
            record.ensure_active()
            result = finished.result
            finish = record.finish
            on_verdict = finish.get("on_verdict")
            if on_verdict is not None:
                payload = {
                    **record.invocation,
                    "result": result,
                    "turn": finished.turn,
                    "verdict": finished.verdict,
                }
                callback_result = cast(Any, on_verdict)(payload)
                if inspect.isawaitable(callback_result):
                    await callback_result
                record.ensure_active()
            return result

        return dispatch

    def install(self) -> None:
        if self._closed:
            raise ValueError("Prepared native adapters are closed")
        if self._installed:
            return
        applied_tools: list[_ToolSlot] = []
        applied_runners: list[tuple[type[object], object, object]] = []
        applied_pydantic: list[tuple[Any, Any]] = []
        try:
            for slot in self._tool_slots:
                slot.validate()
            for native_class, original, _installed in self._runner_slots:
                if vars(native_class).get("run") is not original:
                    raise ValueError("Runner.run changed before configured adapter installation")
            for (
                agent,
                original_run,
                _guard,
                _installed_run,
                _owns_validator,
            ) in self._pydantic_slots:
                current_run = getattr(agent, "run")
                if (
                    "run" in vars(agent)
                    or getattr(current_run, "__self__", None)
                    is not getattr(original_run, "__self__", None)
                    or getattr(current_run, "__func__", None)
                    is not getattr(original_run, "__func__", None)
                ):
                    raise ValueError(
                        "Pydantic Agent.run changed before configured adapter installation"
                    )
            for slot in self._tool_slots:
                slot.install()
                applied_tools.append(slot)
            for native_class, original, installed in self._runner_slots:
                current = vars(native_class).get("run")
                if current is not original:
                    raise ValueError("Runner.run changed before configured adapter installation")
                setattr(native_class, "run", installed)
                applied_runners.append((native_class, original, installed))
            for agent, original_run, guard, _installed_run, _owns_validator in self._pydantic_slots:
                current_run = getattr(agent, "run")
                if (
                    "run" in vars(agent)
                    or getattr(current_run, "__self__", None)
                    is not getattr(original_run, "__self__", None)
                    or getattr(current_run, "__func__", None)
                    is not getattr(original_run, "__func__", None)
                ):
                    raise ValueError(
                        "Pydantic Agent.run changed before configured adapter installation"
                    )
                installed_run = types.MethodType(
                    self._make_pydantic_dispatcher(agent, original_run, guard), agent
                )
                setattr(agent, "run", installed_run)
                owns_validator = guard.install()
                self._pydantic_slots[
                    self._pydantic_slots.index(
                        (agent, original_run, guard, _installed_run, _owns_validator)
                    )
                ] = (agent, original_run, guard, installed_run, owns_validator)
                applied_pydantic.append((agent, installed_run))
        except Exception:
            for agent, installed_run in reversed(applied_pydantic):
                if vars(agent).get("run") is installed_run:
                    delattr(agent, "run")
            for native_class, original, installed in reversed(applied_runners):
                if vars(native_class).get("run") is installed:
                    setattr(native_class, "run", original)
            for slot in reversed(applied_tools):
                slot.restore()
            raise
        self._installed = True

    def _make_pydantic_dispatcher(self, agent: object, original_run: Any, guard: object) -> Any:
        async def dispatch(_agent: object, *args: Any, **kwargs: Any) -> Any:
            from .finish import current_configured_native_finish, mark_configured_native_finish_used

            record = current_configured_native_finish()
            if record is None or record.driver != "pydantic_ai":
                return await original_run(*args, **kwargs)
            record.ensure_active()
            if getattr(record.session, "configured_agent_name", None) != record.agent_name:
                raise CheckError("Configured Pydantic finish session does not match its Agent.")
            descriptor = next(
                (
                    item
                    for item in self.descriptors
                    if item.kind == "pydantic_ai"
                    and item.agent_name == record.agent_name
                    and item.adapter_name == record.adapter_name
                    and item.native_agent is agent
                ),
                None,
            )
            if descriptor is None:
                raise CheckError(
                    "Configured Pydantic finish called Agent.run on an Agent that is not "
                    "bound to the selected adapter"
                )
            if len(args) > 1:
                raise TypeError(
                    "Configured Pydantic finish accepts only the prompt as a positional argument"
                )
            if args:
                prompt = args[0]
                if "user_prompt" in kwargs:
                    raise TypeError("Pydantic prompt was provided more than once")
            else:
                try:
                    prompt = kwargs.pop("user_prompt")
                except KeyError:
                    raise TypeError("Configured Pydantic finish requires a prompt") from None
            mark_configured_native_finish_used(record)
            from .client import NormaClient, new_session_client

            async with new_session_client(NormaClient, record.session) as client:
                finished = await cast(Any, guard).run(
                    prompt,
                    record.recorder,
                    client=client,
                    agent_name=record.agent_name,
                    target_type=record.target_type,
                    target_id=record.target_id,
                    _run_callable=original_run,
                    _ensure_active=record.ensure_active,
                    **kwargs,
                )
            record.ensure_active()
            result = finished.result
            on_verdict = record.finish.get("on_verdict")
            if on_verdict is not None:
                payload = {
                    **record.invocation,
                    "result": result,
                    "turn": finished.turn,
                    "verdict": finished.verdict,
                }
                callback_result = cast(Any, on_verdict)(payload)
                if inspect.isawaitable(callback_result):
                    await callback_result
                record.ensure_active()
            return result

        return dispatch

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for agent, _original, guard, installed_run, owns_validator in reversed(
            self._pydantic_slots
        ):
            if installed_run is not None and vars(agent).get("run") is installed_run:
                delattr(agent, "run")
            if owns_validator:
                guard.uninstall()
        for native_class, original, installed in reversed(self._runner_slots):
            if vars(native_class).get("run") is installed:
                setattr(native_class, "run", original)
        for slot in reversed(self._tool_slots):
            slot.restore()

    async def aclose(self) -> None:
        self.close()


async def prepare_native_adapters(
    descriptors: list[NativeAdapterDescriptor], sessions: dict[str, object]
) -> PreparedNativeAdapters:
    """Prepare verified framework bindings without opening event-loop clients."""
    return PreparedNativeAdapters(descriptors, sessions)


def validate_native_adapter_batch(descriptors: list[NativeAdapterDescriptor]) -> None:
    """Reject duplicate framework mutation owners across a multi-agent plan."""
    agents: dict[int, tuple[object, str, str]] = {}
    tool_lists: dict[int, tuple[list[object], str, str]] = {}
    for descriptor in descriptors:
        previous_agent = agents.get(id(descriptor.native_agent))
        if previous_agent is not None and previous_agent[0] is descriptor.native_agent:
            raise ValueError(
                f"Native Agent is shared by configured adapters "
                f"'{previous_agent[2]}' and '{descriptor.adapter_name}'"
            )
        agents[id(descriptor.native_agent)] = (
            descriptor.native_agent,
            descriptor.agent_name,
            descriptor.adapter_name,
        )
        if descriptor.kind != "openai_agents":
            continue
        native_tools = cast(list[object], getattr(descriptor.native_agent, "tools"))
        previous_list = tool_lists.get(id(native_tools))
        if previous_list is not None and previous_list[0] is native_tools:
            raise ValueError(
                f"OpenAI Agent tools list is shared by configured adapters "
                f"'{previous_list[2]}' and '{descriptor.adapter_name}'"
            )
        tool_lists[id(native_tools)] = (
            native_tools,
            descriptor.agent_name,
            descriptor.adapter_name,
        )
