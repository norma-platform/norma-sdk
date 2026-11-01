# Modified for Norma.
"""Runtime for v2 declarative operation admission and caller observations."""

from __future__ import annotations

import copy
import inspect
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from ._instrumentation_values import (
    _TargetArguments,
    compile_value_source,
    resolve_binding,
    target_arguments_for,
)
from ._observed_state import copy_observed_state
from ._request_context import current_invocation
from ._state import _StateContainer, active_state
from .client import NormaClient, new_session_client
from .models import Step
from .models.operations import (
    AddOperationObservationRequest,
    CreateOperationRequest,
    LinkOperationVerificationRequest,
)


@dataclass
class OperationSpec:
    name: str
    target: Any
    target_arguments: _TargetArguments
    operation_key: Callable[[dict[str, object]], object]
    input_reader: Callable[[dict[str, object]], object]
    context_reader: Callable[[dict[str, object]], object] | None
    evidence_reader: Callable[[dict[str, object]], object] | None
    tools_reader: Callable[[dict[str, object]], object] | None
    action_kind: str | None
    action_readers: dict[str, Callable[[dict[str, object]], object]]
    unresolved_policy: str
    observation_readers: dict[str, dict[str, Callable[[dict[str, object]], object]]]
    verification_reader: Callable[[dict[str, object]], object] | None
    on_record: Callable[..., object] | None


def compile_operations(
    operations: Mapping[str, Any], bindings: Mapping[str, object], resolve_target: Callable
) -> dict[tuple[int, str], OperationSpec]:
    compiled: dict[tuple[int, str], OperationSpec] = {}
    for name, raw_config in operations.items():
        config = dict(raw_config)
        target = resolve_target(config["target"], bindings)
        target_arguments = target_arguments_for(target.raw, target.mode)
        if target.identity in compiled:
            raise ValueError(f"Duplicate operation target slot: {target.path}")
        action_readers: dict[str, Callable[[dict[str, object]], object]] = {}
        action = config.get("action")
        if action is not None:
            for key in ("target", "command"):
                value = action.get(key)
                if isinstance(value, Mapping):
                    action_readers[key] = compile_value_source(
                        value, bindings, target_arguments=target_arguments
                    )
                else:

                    def literal_action_value(
                        _invocation: dict[str, object], selected: object = value
                    ) -> object:
                        return selected

                    action_readers[key] = literal_action_value

        observations: dict[str, dict[str, Callable[[dict[str, object]], object]]] = {}
        for stage, fields in config.get("observations", {}).items():
            observations[stage] = {
                field: _compile_observation_source(
                    source, bindings, stage, target_arguments
                )
                for field, source in fields.items()
            }
        callback: Callable[..., object] | None = None
        if "on_record" in config:
            resolved_callback = resolve_binding(config["on_record"]["binding"], bindings)
            if not callable(resolved_callback):
                raise ValueError("operations.on_record binding must be a function")
            callback = cast(Callable[..., object], resolved_callback)
        input_reader: Callable[[dict[str, object]], object]
        if "input" in config:
            input_reader = compile_value_source(
                config["input"], bindings, target_arguments=target_arguments
            )
        else:
            input_reader = _default_operation_input
        compiled[target.identity] = OperationSpec(
            name=name,
            target=target,
            target_arguments=target_arguments,
            operation_key=compile_value_source(
                config["operation_key"], bindings, target_arguments=target_arguments
            ),
            input_reader=input_reader,
            context_reader=(
                compile_value_source(
                    config["context"], bindings, target_arguments=target_arguments
                )
                if "context" in config
                else None
            ),
            evidence_reader=(
                compile_value_source(
                    config["evidence"], bindings, target_arguments=target_arguments
                )
                if "evidence" in config
                else None
            ),
            tools_reader=(
                compile_value_source(
                    config["tools"], bindings, target_arguments=target_arguments
                )
                if "tools" in config
                else None
            ),
            action_kind=(cast(str, action["kind"]) if action is not None else None),
            action_readers=action_readers,
            unresolved_policy=config.get("unresolved_policy", "block"),
            observation_readers=observations,
            verification_reader=(
                compile_value_source(
                    config["verification_session_id"],
                    bindings,
                    precheck=False,
                    target_arguments=target_arguments,
                )
                if "verification_session_id" in config
                else None
            ),
            on_record=callback,
        )
    return compiled


def operation_wrapper(
    spec: OperationSpec, raw: Callable[..., Any], owner_session: _StateContainer
) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(raw):

        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            copied_args, copied_kwargs = _snapshot_arguments(raw, args, kwargs)
            return await _execute(spec, owner_session, raw, copied_args, copied_kwargs)

        wrapper = async_wrapper
    else:

        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            from .instrumentation import _run_sync

            copied_args, copied_kwargs = _snapshot_arguments(raw, args, kwargs)
            return _run_sync(_execute(spec, owner_session, raw, copied_args, copied_kwargs))

        wrapper = sync_wrapper
    from .instrumentation import functools_update_wrapper

    functools_update_wrapper(wrapper, raw)
    return wrapper


async def _execute(
    spec: OperationSpec,
    owner_session: _StateContainer,
    raw: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    from .checkpoints import CheckError

    session = active_state()
    if session is not owner_session or session.closed:
        raise CheckError("A configured entrypoint scope is required for this operation.")
    try:
        session.require_agent_identity()
    except RuntimeError as exc:
        raise CheckError(str(exc)) from exc
    request_context = current_invocation(session)
    if request_context is None:
        raise CheckError("A configured entrypoint scope is required for this operation.")
    context: dict[str, object] = {
        "args": list(args),
        "kwargs": kwargs,
        "context": request_context.context,
        "agent_name": session.configured_agent_name or "",
        "result": None,
        "error": None,
    }
    parameter_snapshot: dict[str, object] | None = None
    if spec.target_arguments.requires_snapshot:
        from .instrumentation import _snapshot_entrypoint_value

        parameter_snapshot = spec.target_arguments.snapshot(context, _snapshot_entrypoint_value)
    session_generation = session.session_generation
    action_started = False
    try:
        operation_key = await _evaluate(spec.operation_key, context, parameter_snapshot)
        step_input = await _evaluate(spec.input_reader, context, parameter_snapshot)
        if type(step_input) is not dict:
            raise ValueError("operations.input must resolve to a JSON object")
        step_context = (
            await _evaluate(spec.context_reader, context, parameter_snapshot)
            if spec.context_reader is not None
            else None
        )
        if step_context is not None and type(step_context) is not dict:
            raise ValueError("operations.context must resolve to a JSON object")
        evidence = (
            await _evaluate(spec.evidence_reader, context, parameter_snapshot)
            if spec.evidence_reader is not None
            else []
        )
        tools = (
            await _evaluate(spec.tools_reader, context, parameter_snapshot)
            if spec.tools_reader is not None
            else None
        )
        if evidence is not None and type(evidence) is not list:
            raise ValueError("operations.evidence must resolve to a JSON array")
        action: dict[str, object] | None = None
        if spec.action_kind is not None:
            action = {"kind": spec.action_kind}
            for key, reader in spec.action_readers.items():
                value = await _evaluate(reader, context, parameter_snapshot)
                if value is not None and not isinstance(value, str):
                    raise ValueError(f"operations.action.{key} must resolve to a string or null")
                action[key] = value
        if request_context.target_type is None:
            target_type, target_id = session.target_type, session.target_id
        else:
            target_type, target_id = request_context.target_type, request_context.target_id
        context["_target_type"] = target_type
        context["_target_id"] = target_id
        if session.configured_agent_name is None:
            raise ValueError("Configured operation session has no verified Agent name")
        step = Step(
            type="tool",
            name=spec.name,
            input=copy_observed_state(step_input),
            output=None,
            context=copy_observed_state(step_context) if step_context is not None else None,
            tools=_json_snapshot(tools) if tools is not None else None,
            ground_truth=None,
        )
        request = CreateOperationRequest(
            agent_name=session.configured_agent_name,
            operation_key=_required_string(operation_key, "operation_key"),
            step=step,
            target_type=target_type,
            target_id=target_id,
            unresolved_policy=cast(Literal["block", "allow"], spec.unresolved_policy),
            evidence=evidence or [],
        )
        async with new_session_client(NormaClient, session) as client:
            from . import operations

            record = await operations.create(client, request)
            _require_live_owner(owner_session, request_context, session_generation)
            if spec.on_record is not None:
                await _call(
                    spec.on_record,
                    {**context, "operation": record, "stage": "admitted"},
                )
                _require_live_owner(owner_session, request_context, session_generation)
            if not record.decision.allowed or record.decision.outcome != "allow":
                _record_turn_action(spec, action, "denied")
                raise CheckError(record.decision.reason or "Operation was denied.")
            await _observe(client, spec, context, record.id, "attempted", parameter_snapshot)
            _require_live_owner(owner_session, request_context, session_generation)
            try:
                action_started = True
                result = raw(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                _record_turn_action(spec, action, "succeeded", result=result)
            except BaseException as error:
                context["error"] = error
                _record_turn_action(spec, action, "failed", error=error)
                try:
                    await _observe(
                        client, spec, context, record.id, "failed", parameter_snapshot
                    )
                except Exception as report_error:
                    raise error from report_error
                raise
            context["result"] = result
            try:
                await _observe(
                    client, spec, context, record.id, "succeeded", parameter_snapshot
                )
            except Exception as report_error:
                raise RuntimeError(
                    "Operation executed, but its caller-reported success could not be recorded."
                ) from report_error
            if spec.verification_reader is not None:
                try:
                    session_id = await _evaluate(
                        spec.verification_reader, context, parameter_snapshot
                    )
                    await operations.link_verification(
                        client,
                        record.id,
                        LinkOperationVerificationRequest(
                            agent_name=session.configured_agent_name,
                            verification_session_id=str(session_id),
                            target_type=target_type,
                            target_id=target_id,
                        ),
                    )
                except Exception as report_error:
                    raise RuntimeError(
                        "Operation executed, but its verification link could not be recorded."
                    ) from report_error
            return result
    except CheckError:
        raise
    except Exception:
        if action_started:
            raise
        raise CheckError("Operation admission failed; the action was not executed.") from None


async def _observe(
    client: NormaClient,
    spec: OperationSpec,
    invocation: dict[str, object],
    operation_id: str,
    stage: str,
    parameter_snapshot: Mapping[str, object] | None = None,
) -> None:
    readers = spec.observation_readers.get(stage)
    if not readers:
        return
    from . import operations

    values = {
        key: await _evaluate(reader, invocation, parameter_snapshot)
        for key, reader in readers.items()
    }
    session = active_state()
    if session is None or session.configured_agent_name is None:
        return
    await operations.observe(
        client,
        operation_id,
        AddOperationObservationRequest(
            agent_name=session.configured_agent_name,
            event_key=str(values["event_key"]),
            kind=cast(Literal["attempted", "succeeded", "failed"], stage),
            summary=str(values["summary"]),
            external_reference=(
                str(values["external_reference"])
                if values.get("external_reference") is not None
                else None
            ),
            target_type=cast(str | None, invocation.get("_target_type")),
            target_id=cast(str | None, invocation.get("_target_id")),
        ),
    )


async def _evaluate(
    reader: Callable[[dict[str, object]], object],
    context: dict[str, object],
    parameter_snapshot: Mapping[str, object] | None = None,
) -> Any:
    try:
        from ._instrumentation_values import resolve_value_source

        value = resolve_value_source(reader, context, parameter_snapshot)
        if inspect.isawaitable(value):
            value = await value
        return _json_snapshot(value)
    except Exception:
        raise ValueError("Operation value could not be resolved") from None


def _default_operation_input(invocation: dict[str, object]) -> object:
    return {"args": invocation["args"], "kwargs": invocation["kwargs"]}


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"operations.{field} must resolve to a nonblank string")
    return value


def _record_turn_action(
    spec: OperationSpec,
    action: dict[str, object] | None,
    status: Literal["succeeded", "failed", "denied"],
    *,
    result: object = None,
    error: BaseException | None = None,
) -> None:
    if action is None:
        return
    from .finish import _record_decorated_action

    _record_decorated_action(
        spec.name,
        status,
        kind=cast(Any, action["kind"]),
        target=cast(str | None, action.get("target")),
        command=cast(str | None, action.get("command")),
        result=result,
        error=error,
    )


def _compile_observation_source(
    source: Mapping[str, object],
    bindings: Mapping[str, object],
    stage: str,
    target_arguments: _TargetArguments,
) -> Callable[[dict[str, object]], object]:
    if "path" in source:
        path = source["path"]
        allowed_roots = {
            "attempted": {"args", "kwargs", "context", "input", "params"},
            "succeeded": {"args", "kwargs", "context", "input", "params", "result"},
            "failed": {"args", "kwargs", "context", "input", "params", "error"},
        }[stage]
        if isinstance(path, list) and path and path[0] not in allowed_roots:
            raise ValueError(f"operations.observations.{stage} cannot read that value source")
    return compile_value_source(
        source,
        bindings,
        precheck=False,
        target_arguments=target_arguments,
    )


def _json_snapshot(value: object) -> Any:
    return json.loads(json.dumps(value, allow_nan=False))


def _snapshot_arguments(
    raw: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    memo: dict[int, object] = {}
    try:
        parameters = inspect.signature(raw).parameters
        if parameters and next(iter(parameters)) in {"self", "cls"} and args:
            memo[id(args[0])] = args[0]
        copied_args, copied_kwargs = copy.deepcopy((args, kwargs), memo)
        return copied_args, copied_kwargs
    except Exception:
        raise ValueError("Operation arguments could not be safely snapshotted") from None


def _require_live_owner(
    owner_session: _StateContainer, request_context: Any, session_generation: int
) -> None:
    from .checkpoints import CheckError

    if (
        active_state() is not owner_session
        or owner_session.closed
        or owner_session.session_generation != session_generation
        or current_invocation(owner_session) is not request_context
    ):
        raise CheckError("Configured operation session closed or changed before execution.")
    try:
        owner_session.require_agent_identity()
    except RuntimeError as exc:
        raise CheckError(str(exc)) from exc


async def _call(callback: Callable[..., object], payload: dict[str, object]) -> None:
    try:
        value = callback(payload)
        if inspect.isawaitable(value):
            await value
    except Exception:
        raise ValueError("Operation callback failed") from None
