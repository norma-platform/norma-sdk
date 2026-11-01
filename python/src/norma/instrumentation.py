# Modified for Norma.
"""Parse and atomically install declarative checkpoint instrumentation."""

from __future__ import annotations

import asyncio
import inspect
import os
import threading
import types
from collections.abc import Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import copy_context
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from _norma_config.input_schema import InputSchemaError, compile_input_schema

from ._instrumentation_values import (
    ConfiguredSourceResolutionError,
    UnknownTargetParameterError,
    _MissingValuePath,
    _TargetArguments,
    compile_value_source,
    resolve_binding,
    resolve_value_source,
    target_arguments_for,
)
from ._observed_state import copy_observed_state
from ._request_context import use_invocation
from ._state import _StateContainer, use_state
from .agents import _list_agent_rules, get_agent
from .checkpoints import checkpoint
from .client import NormaClient
from .configuration import resolve_agent_key, resolve_environment, resolve_url
from .control_contract import ControlContract, normalize_control_contract
from .models import Agent, StepSchema
from .validation import ensure_agent_name, ensure_step_type

if TYPE_CHECKING:
    from .observability import DeliveryReceipt

from _norma_config.parser import (
    VALIDATOR as _VALIDATOR,
)
from _norma_config.parser import (
    json_safe_config as _json_safe_config,
)
from _norma_config.parser import (
    normalize_turn_aliases as _normalize_turn_aliases,
)
from _norma_config.parser import (
    parse_document as _parse_instrumentation_document,
)

_OPTION_KEYS = {"action_kind", "action_target_arg", "action_command_arg", "action_cwd_arg", "state"}
_MARKER = "__norma_checkpoint_wrapper__"
_MISSING_ENV = object()


def _config_pointer(parts: tuple[str, ...]) -> str:
    return "".join("/" + part.replace("~", "~0").replace("/", "~1") for part in parts)


def parse_instrumentation(text: str, format: Literal["json", "yaml"] = "json") -> dict[str, Any]:
    """Parse and validate JSON or YAML instrumentation without side effects."""
    return _validated_config(_parse_instrumentation_document(text, format))


@dataclass
class _Slot:
    owner: object
    attribute: str
    old: object
    installed: object
    was_present: bool = True
    path: str = ""


@dataclass
class _Resolved:
    path: str
    owner: object
    attribute: str
    previous: object
    raw: Callable[..., Any]
    mode: str
    was_present: bool

    @property
    def identity(self) -> tuple[int, str]:
        return id(self.owner), self.attribute


@dataclass
class _EntryContextSpec:
    context_reader: Callable[[dict[str, object]], object] | None = None
    target_readers: (
        tuple[Callable[[dict[str, object]], object], Callable[[dict[str, object]], object]] | None
    ) = None
    trace_readers: (
        tuple[Callable[[dict[str, object]], object], Callable[[dict[str, object]], object]] | None
    ) = None
    turn: dict[str, Any] | None = None
    requires_copy: bool = False
    target_arguments: _TargetArguments | None = None
    requires_parameter_snapshot: bool = False


_MISSING = object()


def _read_slot(owner: object, name: str) -> object:
    if type(owner) is dict:
        return owner[name]
    return inspect.getattr_static(owner, name)


def _write_slot(owner: object, name: str, value: object) -> None:
    if type(owner) is dict:
        owner[name] = value
    elif isinstance(owner, types.ModuleType):
        vars(owner)[name] = value
    else:
        setattr(owner, name, value)


def _resolve_path(
    path: str, bindings: Mapping[str, object], *, allow_generator: bool = False
) -> _Resolved:
    parts = path.split(".")
    if parts[0] not in bindings:
        raise ValueError(f"Missing binding for target root '{parts[0]}'")
    current: object = bindings[parts[0]]
    if isinstance(current, dict) and type(current) is not dict:
        raise ValueError(f"Target '{path}' uses a dict subclass as a binding root")
    for part in parts[1:-1]:
        if isinstance(current, dict) and type(current) is not dict:
            raise ValueError(f"Target '{path}' traverses a dict subclass")
        if type(current) is dict:
            try:
                current = current[part]
            except KeyError:
                raise ValueError(f"Target '{path}' does not exist") from None
        else:
            try:
                value = inspect.getattr_static(current, part)
            except AttributeError:
                raise ValueError(f"Target '{path}' does not exist") from None
            if isinstance(value, (property, staticmethod, classmethod)):
                raise ValueError(f"Target '{path}' traverses an unsupported descriptor")
            if hasattr(value, "__get__") and not inspect.isfunction(value):
                raise ValueError(f"Target '{path}' traverses an unsupported descriptor")
            current = value
    owner, name = current, parts[-1]
    if isinstance(owner, dict) and type(owner) is not dict:
        raise ValueError(f"Target '{path}' uses a dict subclass as a slot owner")
    if type(owner) is dict:
        pass
    elif isinstance(owner, types.ModuleType):
        pass
    elif isinstance(owner, type):
        if type(owner).__setattr__ is not type.__setattr__:
            raise ValueError(f"Target '{path}' uses a class with custom attribute assignment")
    elif type(owner).__setattr__ is not object.__setattr__:
        raise ValueError(f"Target '{path}' uses custom attribute assignment")
    try:
        previous = _read_slot(owner, name)
    except (AttributeError, KeyError):
        raise ValueError(f"Target '{path}' does not exist") from None
    was_present = True
    if isinstance(owner, dict) or isinstance(owner, types.ModuleType):
        raw, mode = previous, "plain"
    elif isinstance(owner, type):
        descriptor = vars(owner).get(name, _MISSING)
        if descriptor is _MISSING:
            raise ValueError(f"Target '{path}' must be defined directly on its class")
        if isinstance(descriptor, staticmethod):
            raw, mode = descriptor.__func__, "staticmethod"
        elif isinstance(descriptor, classmethod):
            raw, mode = descriptor.__func__, "classmethod"
        elif inspect.isfunction(descriptor):
            raw, mode = descriptor, "class-function"
        else:
            raise ValueError(f"Target '{path}' uses an unsupported descriptor")
    else:
        try:
            instance_values = vars(owner)
        except TypeError:
            instance_values = {}
        if name in instance_values:
            raw, mode = previous, "plain"
        else:
            descriptor = inspect.getattr_static(type(owner), name, _MISSING)
            if inspect.isfunction(descriptor):
                raw, mode, was_present = descriptor, "instance-method", False
            elif isinstance(descriptor, (staticmethod, classmethod)):
                raw = descriptor.__func__
                mode = (
                    "instance-static" if isinstance(descriptor, staticmethod) else "instance-class"
                )
                was_present = False
            else:
                raise ValueError(f"Target '{path}' uses an unsupported descriptor")
    if not callable(raw):
        raise ValueError(f"Target '{path}' is not callable")
    if not (inspect.isfunction(raw) or inspect.ismethod(raw)):
        raise ValueError(f"Target '{path}' must be an ordinary function or bound method")
    if not allow_generator and (
        inspect.isgeneratorfunction(raw) or inspect.isasyncgenfunction(raw)
    ):
        raise ValueError(f"Target '{path}' is a generator; checkpoint execution cannot guard it")
    if getattr(raw, _MARKER, False):
        raise ValueError(f"Target '{path}' is already a Norma checkpoint")
    return _Resolved(path, owner, name, previous, cast(Callable[..., Any], raw), mode, was_present)


def _install(resolved: _Resolved, function: Callable[..., Any]) -> object:
    if _read_slot(resolved.owner, resolved.attribute) is not resolved.previous:
        raise ValueError(f"Target '{resolved.path}' changed during configuration")
    if resolved.mode == "staticmethod":
        installed: object = staticmethod(function)
    elif resolved.mode == "classmethod":
        installed = classmethod(function)
    elif resolved.mode == "instance-method":
        installed = types.MethodType(function, resolved.owner)
    elif resolved.mode == "instance-static":
        installed = function
    elif resolved.mode == "instance-class":
        installed = types.MethodType(function, type(resolved.owner))
    else:
        installed = function
    _write_slot(resolved.owner, resolved.attribute, installed)
    return _read_slot(resolved.owner, resolved.attribute)


def _target_arguments(resolved: _Resolved) -> _TargetArguments:
    """Prepare named-value selectors using the target's actual descriptor mode."""
    return target_arguments_for(resolved.raw, resolved.mode)


def _restore(slot: _Slot) -> None:
    try:
        current = _read_slot(slot.owner, slot.attribute)
        if current is slot.installed:
            if slot.was_present:
                _write_slot(slot.owner, slot.attribute, slot.old)
            else:
                delattr(slot.owner, slot.attribute)
    except (AttributeError, KeyError, TypeError):
        pass


def _ensure_owned_slots(slots: list[_Slot]) -> None:
    """Fence execution when any configured target slot no longer has our wrapper."""
    from .checkpoints import CheckError

    for slot in slots:
        try:
            current = _read_slot(slot.owner, slot.attribute)
        except (AttributeError, KeyError, TypeError):
            current = _MISSING
        if current is not slot.installed:
            target = slot.path or slot.attribute
            raise CheckError(
                f"Configured target '{target}' changed after setup; execution blocked."
            )


def _parse_source(source: object, agent_name: str | None) -> dict[str, dict[str, Any]]:
    if isinstance(source, Mapping):
        if agent_name is None:
            raise ValueError("agent_name is required for in-memory instrumentation config")
        canonical = ensure_agent_name(agent_name)
        if canonical != agent_name:
            raise ValueError("agent_name must already be normalized")
        config = _validated_config(dict(source))
        _reject_unsupported_runtime_sources(config)
        return {canonical: config}
    if agent_name is not None:
        raise ValueError("agent_name cannot override identity from an instrumentation file path")
    if not isinstance(source, (str, os.PathLike)):
        raise ValueError("Instrumentation source must be a config file or directory")
    path = Path(os.fspath(source))
    if path.is_dir():
        selected = sorted(
            (
                candidate
                for candidate in path.iterdir()
                if candidate.is_file() and candidate.suffix.lower() in {".json", ".yaml", ".yml"}
            ),
            key=lambda candidate: candidate.name,
        )
        if not selected:
            raise ValueError("Instrumentation directory contains no .json, .yaml, or .yml files")
    else:
        if not path.is_file() or path.suffix.lower() not in {".json", ".yaml", ".yml"}:
            raise ValueError("Instrumentation source must be a config file or directory")
        selected = [path]
    result: dict[str, dict[str, Any]] = {}
    for file in selected:
        name = file.stem
        if name in result:
            raise ValueError(f"Duplicate instrumentation file stem '{name}'")
        if ensure_agent_name(name) != name:
            raise ValueError(f"Instrumentation file stem '{name}' must be a normalized agent_name")
        fmt: Literal["json", "yaml"] = "json" if file.suffix.lower() == ".json" else "yaml"
        config = parse_instrumentation(file.read_text(encoding="utf-8"), format=fmt)
        _reject_unsupported_runtime_sources(config)
        result[name] = config
    return result


def _reject_unsupported_runtime_sources(config: Mapping[str, Any]) -> None:
    """Reject schema-valid source shorthands the Python runtime cannot bind."""
    if config.get("version") != 2:
        return
    checkpoints = config.get("checkpoints")
    if not isinstance(checkpoints, Mapping):
        return
    for name, checkpoint_config in checkpoints.items():
        if (
            isinstance(checkpoint_config, Mapping)
            and checkpoint_config.get("input_from") == "all_params"
        ):
            raise ValueError(
                f"Unsupported checkpoint input_from at checkpoints.{name}.input_from: "
                "the Python SDK does not support all_params."
            )


def _validated_config(config: dict[str, Any]) -> dict[str, Any]:
    config = _json_safe_config(config)
    errors = list(_VALIDATOR.iter_errors(config))
    if errors:
        candidates: list[Any] = []

        def collect(error: Any) -> None:
            if error.context:
                for child in error.context:
                    collect(child)
            else:
                candidates.append(error)

        for error in errors:
            collect(error)
        error = max(candidates or errors, key=lambda item: len(item.absolute_path))
        path = ".".join(str(part) for part in error.absolute_path) or "$"
        raise ValueError(f"Invalid instrumentation at {path} (constraint: {error.validator}).")
    _normalize_turn_aliases(config)
    file_connection = config.get("connection")
    if isinstance(file_connection, dict) and "control_contract" in file_connection:
        file_connection["control_contract"] = normalize_control_contract(
            file_connection["control_contract"]
        )
    for name, entry in config["checkpoints"].items():
        entry.setdefault("step_type", "tool")
        if "input_schema" in entry:
            try:
                compiled_input_schema = compile_input_schema(entry["input_schema"])
                input_source = entry.get("input_from")
                if isinstance(input_source, Mapping) and "value" in input_source:
                    instance_path = compiled_input_schema.validate(input_source["value"])
                    if instance_path is not None:
                        input_path = _config_pointer(
                            ("checkpoints", str(name), "input_from", "value")
                        )
                        schema_path = _config_pointer(("checkpoints", str(name), "input_schema"))
                        raise ValueError(
                            f"Checkpoint '{name}' literal input at {input_path} does not match "
                            f"{schema_path} (instance at {instance_path or '/'})."
                        )
            except InputSchemaError:
                pointer = _config_pointer(("checkpoints", str(name), "input_schema"))
                raise ValueError(
                    f"Checkpoint '{name}' has an invalid input schema at {pointer}."
                ) from None
        if "state" in entry:
            try:
                entry["state"] = copy_observed_state(entry["state"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid state for checkpoint '{name}': {exc}") from None
    return config


@dataclass
class _AgentPlan:
    name: str
    config: dict[str, Any]
    state: _StateContainer
    checkpoints: dict[tuple[int, str], tuple[str, dict[str, object], _Resolved]]
    entrypoints: list[_Resolved]
    schemas: list[StepSchema]
    explicit_schemas: list[StepSchema]
    existing_schemas: list[StepSchema] | None = None
    publish_schemas: bool = False
    entrypoint_contexts: dict[tuple[int, str], _EntryContextSpec] | None = None
    operations: dict[tuple[int, str], Any] | None = None
    framework_adapters: list[Any] | None = None
    native_adapters: list[Any] | None = None


class ConfiguredAgents:
    """Configured, independently scoped existing Norma Agent sessions."""

    def __init__(
        self,
        sessions: dict[str, _StateContainer],
        schemas: dict[str, list[StepSchema]],
        slots: list[_Slot],
        framework_adapters: list[Any] | None = None,
        native_adapters: Any | None = None,
    ) -> None:
        self._sessions = sessions
        self.schemas = schemas
        self._slots = slots
        self._framework_adapters = framework_adapters or []
        self._native_adapters = native_adapters
        self._closed = False
        self._receipts: dict[str, DeliveryReceipt] = {}

    @property
    def agent_names(self) -> tuple[str, ...]:
        return tuple(self._sessions)

    def refresh(self, agent_name: str) -> list[dict[str, Any]]:
        session = self._session(agent_name)
        return _run_sync(_refresh_session(session))

    def flush(self, agent_name: str) -> DeliveryReceipt:
        session = self._session(agent_name)
        from .observability import sync_flush_observability

        with use_state(session):
            return sync_flush_observability()

    async def aflush(self, agent_name: str) -> DeliveryReceipt:
        session = self._session(agent_name)
        from .observability import flush_observability

        with use_state(session):
            return await flush_observability()

    def _session(self, name: str) -> _StateContainer:
        if self._closed:
            raise ValueError("Configured Agent handle is closed")
        try:
            session = self._sessions[name]
        except KeyError:
            raise ValueError(f"Unknown configured agent '{name}'") from None
        session.require_agent_identity()
        return session

    @contextmanager
    def scope(self, agent_name: str) -> Iterator[None]:
        """Select one Agent for native framework setup or explicit SDK calls."""
        session = self._session(agent_name)
        if session.closed:
            raise ValueError(f"Configured Agent '{agent_name}' is closed")
        _ensure_owned_slots(session.instrumentation_slots)
        with use_state(session):
            yield

    def client(self, agent_name: str) -> NormaClient:
        """Create an explicit client with this Agent session's connection settings."""
        session = self._session(agent_name)
        if session.closed:
            raise ValueError(f"Configured Agent '{agent_name}' is closed")
        from .client import new_session_client

        return new_session_client(NormaClient, session)

    def close(self) -> dict[str, DeliveryReceipt]:
        """Stop sessions, restore owned targets, and return per-agent delivery receipts.

        This synchronous close is idempotent. It prevents new scoped work, fences
        pending checks before their action runs, and leaves caller-owned sinks open.
        """
        if self._closed:
            return dict(self._receipts)
        self._closed = True
        for session in self._sessions.values():
            _deactivate_session(session)
        self._close_adapters_sync()
        for name, session in self._sessions.items():
            self._receipts[name] = _close_session_resources(session)
        for slot in reversed(self._slots):
            _restore(slot)
        for session in self._sessions.values():
            session.instrumentation_slots.clear()
        return dict(self._receipts)

    def uninstall(self) -> dict[str, DeliveryReceipt]:
        """Alias for :meth:`close`; return its per-agent delivery receipts."""
        return self.close()

    async def aclose(self) -> dict[str, DeliveryReceipt]:
        """Asynchronously stop sessions and return per-agent delivery receipts."""
        if self._closed:
            return dict(self._receipts)
        self._closed = True
        for session in self._sessions.values():
            _deactivate_session(session)
        await self._close_adapters_async()
        for name, session in self._sessions.items():
            _stop_session_refresh(session)
            from .observability import shutdown_observability

            with use_state(session):
                self._receipts[name] = await shutdown_observability()
        for slot in reversed(self._slots):
            _restore(slot)
        for session in self._sessions.values():
            session.instrumentation_slots.clear()
        return dict(self._receipts)

    def _close_adapters_sync(self) -> None:
        for adapter in reversed(self._framework_adapters):
            try:
                _run_sync(adapter.aclose())
            except Exception:
                continue
        if self._native_adapters is not None:
            try:
                _run_sync(self._native_adapters.aclose())
            except Exception:
                return

    async def _close_adapters_async(self) -> None:
        for adapter in reversed(self._framework_adapters):
            try:
                await adapter.aclose()
            except Exception:
                continue
        if self._native_adapters is not None:
            try:
                await self._native_adapters.aclose()
            except Exception:
                return

    def __enter__(self) -> ConfiguredAgents:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _connection_settings(
    name: str,
    config: Mapping[str, Any],
    bindings: Mapping[str, object],
    connection: Mapping[str, object] | None,
    connections: Mapping[str, Mapping[str, object]] | None,
    known_agents: set[str],
) -> dict[str, object]:
    file_connection = dict(config.get("connection", {}))
    env_refs: dict[str, list[str]] = {}
    file_values: dict[str, object] = {}
    unsupported = set(file_connection) & {"http_client", "debug_logger", "user_agent"}
    if unsupported:
        raise ValueError(
            "Python instrumentation does not support connection option(s): "
            + ", ".join(sorted(unsupported))
        )
    for key, value in file_connection.items():
        if key in {"runtime_url", "agent_key", "transport"} and isinstance(value, Mapping):
            if "env" in value:
                env_name = cast(str, value["env"])
                file_values[key] = os.environ.get(env_name, _MISSING_ENV)
                if file_values[key] is _MISSING_ENV:
                    env_refs.setdefault(key, []).append(env_name)
            elif "binding" in value:
                file_values[key] = resolve_binding(cast(str, value["binding"]), bindings)
        else:
            file_values[key] = value
    if "timeout_ms" in file_values:
        file_values["timeout_ms"] = cast(int, file_values["timeout_ms"])
    telemetry = dict(config.get("telemetry", {}))
    telemetry_names = {
        "enabled": "observability_enabled",
        "sink_name": "observability_sink_name",
        "sink_config": "observability_sink_config",
        **{
            key: key
            for key in (
                "batch_size",
                "flush_interval",
                "max_retries",
                "retry_delay",
                "shutdown_join_timeout",
                "shutdown_flush_timeout",
                "shutdown_max_failed_flushes",
                "otel_enabled",
                "otel_endpoint",
                "otel_headers",
                "otel_service_name",
                "otel_tracer_provider",
            )
        },
    }
    telemetry_values: dict[str, object] = {}
    logging_config: object | None = None
    for key, value in telemetry.items():
        if key == "logging":
            logging_settings = cast(Mapping[str, object], value)
            for setting in (
                "log_enabled",
                "log_span_start",
                "log_span_end",
                "log_rule_eval",
                "log_span_results",
            ):
                if setting in logging_settings:
                    telemetry_values[setting] = logging_settings[setting]
            if "config" in logging_settings:
                logging_config_binding = cast(Mapping[str, object], logging_settings["config"])
                logging_config = resolve_binding(
                    cast(str, logging_config_binding["binding"]), bindings
                )
                if type(logging_config) is not dict:
                    raise ValueError("telemetry.logging.config binding must resolve to a dict")
            continue
        target_key = telemetry_names.get(key)
        if target_key is None:
            raise ValueError(f"Unsupported telemetry option '{key}' in Python configuration")
        if key == "otel_tracer_provider":
            telemetry_values[target_key] = resolve_binding(cast(str, value["binding"]), bindings)
        elif key == "otel_endpoint" and isinstance(value, Mapping):
            env_name = cast(str, value["env"])
            telemetry_values[target_key] = os.environ.get(env_name, _MISSING_ENV)
            if telemetry_values[target_key] is _MISSING_ENV:
                env_refs.setdefault(target_key, []).append(env_name)
        elif key == "otel_headers":
            headers: dict[str, object] = {}
            for header, item in cast(Mapping[str, object], value).items():
                if isinstance(item, Mapping):
                    env_name = cast(str, item["env"])
                    headers[header] = os.environ.get(env_name, _MISSING_ENV)
                    if headers[header] is _MISSING_ENV:
                        env_refs.setdefault(target_key, []).append(env_name)
                else:
                    headers[header] = item
            telemetry_values[target_key] = headers
        else:
            telemetry_values[target_key] = value
    refresh = cast(Mapping[str, Any], config.get("refresh", {}))
    refresh_values = {"refresh_interval_seconds": refresh["interval_seconds"]} if refresh else {}
    shared = file_values | telemetry_values | refresh_values | dict(connection or {})
    per_agent = dict(connections or {})
    unknown = set(per_agent) - known_agents
    if unknown:
        raise ValueError(
            f"Connection override names unknown agent(s): {', '.join(sorted(unknown))}"
        )
    allowed = {
        "runtime_url",
        "agent_key",
        "transport",
        "timeout_ms",
        "target_type",
        "target_id",
        "control_contract",
        "observability_enabled",
        "observability_sink_name",
        "observability_sink_config",
        "refresh_interval_seconds",
        "batch_size",
        "flush_interval",
        "otel_tracer_provider",
        "otel_enabled",
        "otel_endpoint",
        "otel_headers",
        "otel_service_name",
        "max_retries",
        "retry_delay",
        "shutdown_join_timeout",
        "shutdown_flush_timeout",
        "shutdown_max_failed_flushes",
        "log_enabled",
        "log_span_start",
        "log_span_end",
        "log_rule_eval",
        "log_span_results",
    }
    unexpected = (set(shared) | set(per_agent.get(name, {}))) - allowed
    if unexpected:
        raise ValueError(f"Unknown connection option(s): {', '.join(sorted(unexpected))}")
    resolved = shared | dict(per_agent.get(name, {}))

    def contains_missing(value: object) -> bool:
        if value is _MISSING_ENV:
            return True
        if isinstance(value, Mapping):
            return any(contains_missing(item) for item in value.values())
        if type(value) in (list, tuple):
            sequence = cast(list[object] | tuple[object, ...], value)
            return any(contains_missing(item) for item in sequence)
        return False

    missing_env = [key for key, value in resolved.items() if contains_missing(value)]
    if missing_env:
        env_names = [env_name for key in missing_env for env_name in env_refs.get(key, [key])]
        raise ValueError(
            "Missing required instrumentation environment variable(s): " + ", ".join(env_names)
        )
    if (resolved.get("target_type") is None) != (resolved.get("target_id") is None):
        raise ValueError("target_type and target_id must be supplied together")
    resolved["control_contract"] = normalize_control_contract(
        cast(str | None, resolved.get("control_contract"))
    )
    refresh_interval = resolved.get("refresh_interval_seconds", 60)
    if type(refresh_interval) is not int or cast(int, refresh_interval) < 0:
        raise ValueError("refresh_interval_seconds must be a nonnegative integer")
    runtime_url = resolved.get("runtime_url")
    if runtime_url is not None and type(runtime_url) is not str:
        raise ValueError("runtime_url must resolve to a string")
    resolved["runtime_url"] = resolve_url(cast(str | None, runtime_url))
    agent_key = resolved.get("agent_key")
    if agent_key is not None and type(agent_key) is not str:
        raise ValueError("agent_key must resolve to a string")
    resolved["agent_key"] = resolve_agent_key(cast(str | None, agent_key))
    if "timeout_ms" in resolved:
        if type(resolved["timeout_ms"]) is not int or cast(int, resolved["timeout_ms"]) < 1:
            raise ValueError("timeout_ms must be a positive integer")
        resolved["timeout"] = cast(int, resolved["timeout_ms"]) / 1000
    else:
        resolved["timeout"] = 30.0
    if resolved.get("transport") is not None:
        import httpx

        if not isinstance(resolved["transport"], httpx.AsyncBaseTransport):
            raise ValueError("transport must be an httpx.AsyncBaseTransport")
    from .settings import get_settings

    settings_updates = {
        key: value
        for key, value in resolved.items()
        if key
        in {
            "observability_enabled",
            "observability_sink_name",
            "observability_sink_config",
            "batch_size",
            "flush_interval",
            "otel_enabled",
            "otel_endpoint",
            "otel_headers",
            "otel_service_name",
            "max_retries",
            "retry_delay",
            "shutdown_join_timeout",
            "shutdown_flush_timeout",
            "shutdown_max_failed_flushes",
            "log_enabled",
            "log_span_start",
            "log_span_end",
            "log_rule_eval",
            "log_span_results",
        }
    }
    try:
        resolved["validated_settings"] = type(get_settings())(
            **(get_settings().model_dump() | settings_updates)
        )
    except Exception as exc:
        from pydantic import ValidationError

        if isinstance(exc, ValidationError):
            fields = sorted(
                ".".join(str(part) for part in error.get("loc", ()))
                for error in exc.errors(
                    include_input=False, include_context=False, include_url=False
                )
            )
            raise ValueError(
                "Invalid instrumentation settings for field(s): " + ", ".join(fields)
            ) from None
        raise ValueError("Instrumentation settings could not be validated") from None
    resolved["_logging_config"] = logging_config
    from .settings import SDKSettings

    validated_settings = cast(SDKSettings, resolved["validated_settings"])
    sink_name = validated_settings.observability_sink_name
    if sink_name != "default":
        from .observability import (
            _named_event_sink_factories,
            get_registered_rule_event_sinks,
        )

        if sink_name == "registered":
            if not get_registered_rule_event_sinks():
                raise ValueError("The registered observability sink selection has no sinks")
        elif sink_name not in _named_event_sink_factories.registered_names():
            raise ValueError("No observability sink is registered for the configured sink name")
    return resolved


def _build_plan(
    name: str,
    config: dict[str, Any],
    bindings: Mapping[str, object],
    providers: Mapping[str, Callable[..., object]],
    options: Mapping[str, Mapping[str, object]],
) -> _AgentPlan:
    checkpoints: dict[tuple[int, str], tuple[str, dict[str, object], _Resolved]] = {}
    entries: dict[tuple[int, str], _Resolved] = {}
    entrypoint_contexts: dict[tuple[int, str], _EntryContextSpec] = {}
    schemas: list[StepSchema] = []
    for step_name, item_obj in config["checkpoints"].items():
        item = dict(item_obj)
        if "parameter_names" in item:
            raise ValueError(
                "parameter_names is a TypeScript-only setting; Python derives target parameters "
                "from the callable signature"
            )
        resolved = _resolve_path(item["target"], bindings)
        if resolved.identity in checkpoints:
            raise ValueError(f"Duplicate checkpoint target slot for '{name}': {resolved.path}")
        if "state_provider" in item:
            provider_key = item["state_provider"]
            provider = providers.get(provider_key)
            if not callable(provider):
                pointer = _config_pointer(("checkpoints", step_name, "state_provider"))
                raise ValueError(
                    f"Checkpoint '{step_name}' source at {pointer} must reference an exact "
                    "callable key in the providers registry."
                )
            item["state"] = provider
        if "options" in item:
            option_value = item["options"]
            if isinstance(option_value, Mapping):
                if config["version"] != 2:
                    raise ValueError("Inline checkpoint options require instrumentation version 2")
                native = _resolve_inline_options(item, option_value, bindings)
            else:
                option_name = option_value
                if option_name not in options or not isinstance(options[option_name], Mapping):
                    raise ValueError(f"Missing named checkpoint options object '{option_name}'")
                native = dict(options[option_name])
        else:
            native = {}
        unexpected = set(native) - _OPTION_KEYS
        if unexpected:
            raise ValueError(
                f"Unknown native checkpoint option(s): {', '.join(sorted(unexpected))}"
            )
        if "state" in item and "state" in native:
            raise ValueError(f"Checkpoint '{step_name}' state conflicts with native state option")
        if "state" in item:
            native["state"] = item["state"]
        elif "state" in native and not callable(native["state"]):
            native["state"] = copy_observed_state(native["state"])
        target_arguments = _target_arguments(resolved)
        item["instrumentation_target_arguments"] = target_arguments
        if config["version"] == 2:
            pre_resolver, post_resolver = _compile_checkpoint_sources(
                item, bindings, target_arguments, checkpoint_name=step_name
            )
            item["instrumentation_resolver"] = pre_resolver
            item["instrumentation_post_resolver"] = post_resolver
            if "input_schema" in item:
                item["instrumentation_input_schema"] = compile_input_schema(item["input_schema"])
        item["native_options"] = native
        item["step_type"] = ensure_step_type(cast(str, item.get("step_type", "tool")))
        step_type = cast(str, item["step_type"])
        action_kind = native.get("action_kind")
        if action_kind not in {
            None,
            "shell",
            "edit",
            "read",
            "search",
            "web",
            "mcp",
            "task",
            "other",
        }:
            raise ValueError(f"Invalid action_kind for checkpoint '{step_name}'")
        from ._schema_derivation import derive_schemas

        derived = derive_schemas(
            resolved.raw,
            input_signature=target_arguments.application_signature,
        )
        description = (resolved.raw.__doc__ or "").strip().splitlines()
        schema: dict[str, Any] = {
            "type": step_type,
            "name": step_name,
            "input_schema": derived.input_schema,
            "output_schema": derived.output_schema,
        }
        if description and description[0].strip():
            schema["description"] = description[0].strip()
        schemas.append(StepSchema.model_validate(schema))
        checkpoints[resolved.identity] = (step_name, item, resolved)
    for entrypoint in config["entrypoints"]:
        if isinstance(entrypoint, str):
            path = entrypoint
            entry_spec = None
        else:
            if "parameter_names" in entrypoint:
                raise ValueError(
                    "parameter_names is a TypeScript-only setting; Python derives target "
                    "parameters from the callable signature"
                )
            path = cast(str, entrypoint["target"])
        resolved = _resolve_path(path, bindings, allow_generator=config["version"] == 2)
        if not isinstance(entrypoint, str):
            entry_spec = _compile_entrypoint_context(
                cast(dict[str, Any], entrypoint), bindings, _target_arguments(resolved)
            )
        if resolved.identity in entries:
            raise ValueError(f"Duplicate entrypoint target slot for '{name}': {path}")
        if resolved.identity in checkpoints and config["version"] != 2:
            raise ValueError(f"Target '{path}' cannot be both checkpoint and entrypoint")
        entries[resolved.identity] = resolved
        if entry_spec is not None:
            entrypoint_contexts[resolved.identity] = entry_spec
        is_stream = inspect.isgeneratorfunction(resolved.raw) or inspect.isasyncgenfunction(
            resolved.raw
        )
        finish_spec = (
            entry_spec.turn.get("finish")
            if entry_spec is not None and entry_spec.turn is not None
            else None
        )
        if isinstance(finish_spec, Mapping) and "stream" in finish_spec and not is_stream:
            raise ValueError("turn.finish.stream requires a generator entrypoint")
        if is_stream and isinstance(finish_spec, Mapping) and "stream" not in finish_spec:
            raise ValueError("Generator entrypoints require turn.finish.stream")
    from ._instrumentation_operations import compile_operations

    if any("parameter_names" in operation for operation in config.get("operations", {}).values()):
        raise ValueError(
            "parameter_names is a TypeScript-only setting; Python derives target parameters "
            "from the callable signature"
        )
    operations = compile_operations(config.get("operations", {}), bindings, _resolve_path)
    for operation in config.get("operations", {}).values():
        if "returns" in operation:
            raise ValueError("operations.returns is a TypeScript-only declaration")
    schema_config = cast(Mapping[str, object], config.get("schemas", {}))
    explicit_steps = cast(list[dict[str, Any]], schema_config.get("steps", []))
    explicit_by_key: dict[tuple[str, str], StepSchema] = {}
    for step in explicit_steps:
        parsed_step = StepSchema.model_validate(step)
        key = (parsed_step.type, parsed_step.name)
        if key in explicit_by_key:
            raise ValueError(
                f"Duplicate explicit step schema for Agent '{name}', {key[0]}:{key[1]}"
            )
        explicit_by_key[key] = parsed_step
    merged_schemas = {(step.type, step.name): step for step in schemas}
    merged_schemas.update(explicit_by_key)
    schemas = list(merged_schemas.values())
    framework_adapters: list[Any] = []
    if config.get("adapters"):
        from ._instrumentation_framework_adapters import compile_framework_adapters

        framework_adapters, adapter_schemas = compile_framework_adapters(name, config, bindings)
        discovered = [StepSchema.model_validate(step) for step in adapter_schemas]
        merged_schemas = {(step.type, step.name): step for step in schemas}
        for discovered_step in discovered:
            merged_schemas.setdefault((discovered_step.type, discovered_step.name), discovered_step)
        schemas = list(merged_schemas.values())
    native_adapters: list[Any] = []
    native_kinds = {"openai_agents", "pydantic_ai"}
    configured_adapters = cast(Mapping[str, Mapping[str, Any]], config.get("adapters", {}))
    native_finish_requested = any(
        isinstance(entry, Mapping)
        and isinstance(entry.get("turn"), Mapping)
        and isinstance(entry["turn"].get("finish"), Mapping)
        and entry["turn"]["finish"].get("driver") in native_kinds
        for entry in config.get("entrypoints", [])
    )
    if native_finish_requested or any(
        adapter.get("kind") in native_kinds for adapter in configured_adapters.values()
    ):
        from ._instrumentation_native_finish import compile_native_adapters

        native_adapters = compile_native_adapters(config, bindings, name)
    for adapter in native_adapters:
        for raw_step in getattr(adapter, "schemas", ()):
            native_step = StepSchema.model_validate(raw_step)
            key = (native_step.type, native_step.name)
            if key in explicit_by_key and explicit_by_key[key] != native_step:
                raise ValueError(
                    f"Explicit step schema conflicts with native adapter step {key[0]}:{key[1]}"
                )
            existing = next((item for item in schemas if (item.type, item.name) == key), None)
            if existing is not None and existing != native_step:
                raise ValueError(f"Conflicting discovered step schema for {key[0]}:{key[1]}")
            if existing is None:
                schemas.append(native_step)
    return _AgentPlan(
        name=name,
        config=config,
        state=_StateContainer(),
        checkpoints=checkpoints,
        entrypoints=list(entries.values()),
        schemas=schemas,
        explicit_schemas=list(explicit_by_key.values()),
        publish_schemas=bool(schema_config.get("publish", False)),
        entrypoint_contexts=entrypoint_contexts,
        operations=operations,
        framework_adapters=framework_adapters,
        native_adapters=native_adapters,
    )


def _resolve_inline_options(
    item: dict[str, Any],
    options: Mapping[str, Any],
    bindings: Mapping[str, object],
) -> dict[str, object]:
    unsupported = set(options) & {
        "client",
        "stepName",
        "stepType",
        "targetType",
        "targetId",
    }
    if unsupported:
        raise ValueError(
            "Inline checkpoint option(s) are unsupported in Python: "
            + ", ".join(sorted(unsupported))
        )
    allowed = _OPTION_KEYS | {"action"}
    unknown = set(options) - allowed
    if unknown:
        raise ValueError(f"Unknown inline checkpoint option(s): {', '.join(sorted(unknown))}")
    native = {key: value for key, value in options.items() if key in _OPTION_KEYS}
    if "state" in options:
        value = options["state"]
        if isinstance(value, Mapping) and "binding" in value:
            value = resolve_binding(cast(str, value["binding"]), bindings)
        if not callable(value):
            value = copy_observed_state(value)
        native["state"] = value
    if "action" in options:
        if "action" in item:
            raise ValueError("Inline options action conflicts with checkpoint action")
        action = options["action"]
        if isinstance(action, Mapping) and "binding" in action:
            action = resolve_binding(cast(str, action["binding"]), bindings)
        if not isinstance(action, Mapping):
            raise ValueError("Inline action binding must resolve to an action mapping")
        item["action"] = dict(action)
    return native


def _compile_entrypoint_context(
    entrypoint: dict[str, Any],
    bindings: Mapping[str, object],
    target_arguments: _TargetArguments,
) -> _EntryContextSpec:
    requires_copy = False
    requires_parameter_snapshot = False

    def compile_scope(source: Mapping[str, Any], *, allow_context: bool) -> Callable[..., object]:
        nonlocal requires_copy, requires_parameter_snapshot
        path = source.get("path")
        if path and not allow_context and path[0] == "context":
            raise ValueError("Entrypoint context cannot depend on its own context value")
        if path and path[0] in {"input", "params"}:
            requires_parameter_snapshot = True
        if "provider" in source or (path and path[0] in {"args", "kwargs", "input", "params"}):
            requires_copy = True
        return compile_value_source(source, bindings, target_arguments=target_arguments)

    context_reader = (
        compile_scope(cast(Mapping[str, Any], entrypoint["context"]), allow_context=False)
        if "context" in entrypoint
        else None
    )
    target_readers = None
    if "target_scope" in entrypoint:
        target = cast(Mapping[str, Any], entrypoint["target_scope"])
        target_readers = (
            compile_scope(cast(Mapping[str, Any], target["type"]), allow_context=True),
            compile_scope(cast(Mapping[str, Any], target["id"]), allow_context=True),
        )
    trace_readers = None
    if "trace" in entrypoint:
        trace = cast(Mapping[str, Any], entrypoint["trace"])
        trace_readers = (
            compile_scope(cast(Mapping[str, Any], trace["trace_id"]), allow_context=True),
            compile_scope(cast(Mapping[str, Any], trace["span_id"]), allow_context=True),
        )
    turn_config = None
    if "turn" in entrypoint:
        turn = cast(dict[str, Any], entrypoint["turn"])
        turn_config = {
            key: value
            for key, value in turn.items()
            if key in {"platform", "platform_version", "model", "max_continuations"}
        }
        turn_config["session_id_reader"] = compile_scope(
            cast(Mapping[str, Any], turn["session_id"]), allow_context=True
        )
        turn_config["turn_id_reader"] = compile_scope(
            cast(Mapping[str, Any], turn["turn_id"]), allow_context=True
        )
        if "user_request" in turn:
            turn_config["user_request_reader"] = compile_scope(
                cast(Mapping[str, Any], turn["user_request"]), allow_context=True
            )
        finish = cast(dict[str, Any] | None, turn.get("finish"))
        if finish is not None:
            driver = finish.get("driver", "generic")
            if driver == "generic":
                if "stream" in finish:
                    stream = cast(dict[str, Any], finish["stream"])
                    compiled_stream: dict[str, Any] = {
                        "mode": stream.get("mode", "concat"),
                        "max_chars": stream.get("max_chars", 8000),
                        "missing": stream.get("missing", "error"),
                    }
                    if "text" in stream:
                        text_source = cast(Mapping[str, Any], stream["text"])
                        compiled_stream["text_reader"] = compile_value_source(
                            text_source,
                            bindings,
                            precheck=False,
                            target_arguments=target_arguments,
                        )
                        if "path" in text_source and text_source["path"][0] in {
                            "input",
                            "params",
                        }:
                            requires_parameter_snapshot = True
                        if "provider" in text_source or (
                            "path" in text_source
                            and text_source["path"][0] in {"args", "kwargs", "input", "params"}
                        ):
                            requires_copy = True
                        compiled_stream["text_is_path"] = "path" in text_source
                    compiled_finish = {
                        "driver": "generic",
                        "on_hold": finish["on_hold"],
                        "stream": compiled_stream,
                    }
                    callback_names: tuple[str, ...] = ("on_verdict",)
                else:
                    compiled_finish = {
                        "driver": "generic",
                        "on_hold": finish["on_hold"],
                        "final_message_reader": compile_value_source(
                            cast(Mapping[str, Any], finish["final_message"]),
                            bindings,
                            precheck=False,
                            target_arguments=target_arguments,
                        ),
                    }
                    final_message = cast(Mapping[str, Any], finish["final_message"])
                    if "path" in final_message and final_message["path"][0] in {
                        "input",
                        "params",
                    }:
                        requires_parameter_snapshot = True
                    if "provider" in finish["final_message"] or (
                        "path" in finish["final_message"]
                        and finish["final_message"]["path"][0]
                        in {"args", "kwargs", "input", "params"}
                    ):
                        requires_copy = True
                    callback_names = ("continue_with", "on_verdict")
                for callback_name in callback_names:
                    if callback_name in finish:
                        callback = resolve_binding(
                            cast(str, finish[callback_name]["binding"]), bindings
                        )
                        if not callable(callback):
                            raise ValueError(
                                f"turn.finish.{callback_name} binding must be a function"
                            )
                        compiled_finish[callback_name] = callback
                if finish["on_hold"] == "return" and "on_verdict" not in compiled_finish:
                    raise ValueError("turn.finish on_hold='return' requires on_verdict")
                turn_config["finish"] = compiled_finish
            else:
                compiled_finish = {**finish, "driver": driver}
                if "on_verdict" in finish:
                    callback = resolve_binding(cast(str, finish["on_verdict"]["binding"]), bindings)
                    if not callable(callback):
                        raise ValueError("turn.finish.on_verdict binding must be a function")
                    compiled_finish["on_verdict"] = callback
                turn_config["finish"] = compiled_finish
    return _EntryContextSpec(
        context_reader,
        target_readers,
        trace_readers,
        turn_config,
        requires_copy,
        target_arguments,
        requires_parameter_snapshot,
    )


def _compile_checkpoint_sources(
    item: dict[str, Any],
    bindings: Mapping[str, object],
    target_arguments: _TargetArguments,
    *,
    checkpoint_name: str,
) -> tuple[
    Callable[..., Any] | None,
    Callable[..., Any] | None,
]:
    """Resolve v2 providers/binding paths at startup; evaluate selectors per call."""
    source_names = {
        "input": "input_from",
        "context": "context_from",
        "tools": "tools_from",
        "ground_truth": "ground_truth_from",
    }
    payload_readers = {}
    snapshot_failure: tuple[str, str] | None = None

    def compile_source(
        source: Mapping[str, Any],
        field: str,
        *,
        pointer: str | None = None,
        precheck: bool = True,
    ) -> Callable[[dict[str, object]], Any]:
        nonlocal snapshot_failure
        source_pointer = pointer or _config_pointer(("checkpoints", checkpoint_name, field))
        path = source.get("path")
        if (
            snapshot_failure is None
            and isinstance(path, list)
            and path
            and path[0] in {"input", "params"}
        ):
            snapshot_failure = (field, source_pointer)
        try:
            return compile_value_source(
                source,
                bindings,
                precheck=precheck,
                target_arguments=target_arguments,
            )
        except UnknownTargetParameterError as exc:
            raise ValueError(
                f"Checkpoint '{checkpoint_name}' source at {source_pointer} names unknown "
                f"target parameter '{exc.parameter_name}'."
            ) from None
        except Exception:
            expectation = (
                "a callable dotted binding reference in bindings (ValueSource provider)"
                if set(source) == {"provider"}
                else "a valid ValueSource"
            )
            raise ValueError(
                f"Checkpoint '{checkpoint_name}' source at {source_pointer} must be {expectation}."
            ) from None

    def resolve_source(
        reader: Callable[..., Any],
        invocation: dict[str, object],
        parameter_snapshot: Mapping[str, object] | None,
        *,
        field: str,
        pointer: str,
    ) -> object:
        try:
            value = resolve_value_source(reader, invocation, parameter_snapshot)
            return value
        except ConfiguredSourceResolutionError:
            raise
        except Exception:
            raise ConfiguredSourceResolutionError(field, pointer) from None

    for field, source_name in source_names.items():
        if source_name not in item:
            continue
        source = item[source_name]
        if field == "input" and source == "all_kwargs":
            source = {"path": ["kwargs"]}
        payload_readers[field] = compile_source(source, source_name)
    output_pointer = _config_pointer(("checkpoints", checkpoint_name, "output_from"))
    output_reader = (
        compile_source(item["output_from"], "output_from", precheck=False)
        if "output_from" in item
        else None
    )
    state_source = item.get("state_from")
    state_pointer = _config_pointer(("checkpoints", checkpoint_name, "state_from"))
    state_reader = compile_source(state_source, "state_from") if state_source is not None else None
    action = item.get("action")
    if action is not None and not isinstance(action, Mapping):
        pointer = _config_pointer(("checkpoints", checkpoint_name, "action"))
        raise ValueError(f"Checkpoint '{checkpoint_name}' action at {pointer} must be an object.")
    action_readers: dict[str, Callable[[dict[str, object]], object]] = {}
    if action is not None:
        for key in ("target", "command", "cwd"):
            value = action.get(key)
            if isinstance(value, Mapping):
                action_readers[key] = compile_source(
                    value,
                    f"action.{key}",
                    pointer=_config_pointer(("checkpoints", checkpoint_name, "action", key)),
                )
            else:

                def literal_action_value(
                    _invocation: dict[str, object], selected: object = value
                ) -> object:
                    return selected

                action_readers[key] = literal_action_value

    if not (
        state_reader or action is not None or payload_readers or target_arguments.requires_snapshot
    ):
        pre_resolver = None
    else:

        async def resolve(invocation: dict[str, object]) -> dict[str, object]:
            resolved: dict[str, object] = {}
            try:
                parameter_snapshot = (
                    target_arguments.snapshot(invocation, _snapshot_entrypoint_value)
                    if target_arguments.requires_snapshot
                    else None
                )
            except Exception:
                if snapshot_failure is not None:
                    raise ConfiguredSourceResolutionError(*snapshot_failure) from None
                raise
            if parameter_snapshot is not None:
                resolved["_parameter_snapshot"] = parameter_snapshot
            if state_reader is not None:
                value = resolve_source(
                    state_reader,
                    invocation,
                    parameter_snapshot,
                    field="state_from",
                    pointer=state_pointer,
                )
                if inspect.isawaitable(value):
                    try:
                        value = await value
                    except Exception:
                        raise ConfiguredSourceResolutionError("state_from", state_pointer) from None
                try:
                    resolved["state"] = copy_observed_state(value)
                except Exception:
                    raise ConfiguredSourceResolutionError("state_from", state_pointer) from None
            if action is not None:
                action_values: dict[str, object] = {"kind": action["kind"]}
                for key, reader in action_readers.items():
                    pointer = _config_pointer(("checkpoints", checkpoint_name, "action", key))
                    value = resolve_source(
                        reader,
                        invocation,
                        parameter_snapshot,
                        field=f"action/{key}",
                        pointer=pointer,
                    )
                    if inspect.isawaitable(value):
                        try:
                            value = await value
                        except Exception:
                            raise ConfiguredSourceResolutionError(
                                f"action/{key}", pointer
                            ) from None
                    action_values[key] = value
                resolved["action"] = action_values
            if payload_readers:
                payload: dict[str, object] = {}
                for field, reader in payload_readers.items():
                    source_name = source_names[field]
                    pointer = _config_pointer(("checkpoints", checkpoint_name, source_name))
                    value = resolve_source(
                        reader,
                        invocation,
                        parameter_snapshot,
                        field=source_name,
                        pointer=pointer,
                    )
                    if inspect.isawaitable(value):
                        try:
                            value = await value
                        except Exception:
                            raise ConfiguredSourceResolutionError(source_name, pointer) from None
                    try:
                        payload[field] = _validate_checkpoint_payload_value(
                            field, value, pointer=pointer
                        )
                    except ConfiguredSourceResolutionError:
                        raise
                    except Exception:
                        raise ConfiguredSourceResolutionError(source_name, pointer) from None
                resolved["payload"] = payload
            return resolved

        pre_resolver = resolve

    if output_reader is None:
        post_resolver = None
    else:

        async def resolve_output(
            invocation: dict[str, object],
            parameter_snapshot: Mapping[str, object] | None = None,
        ) -> dict[str, object]:
            value = resolve_source(
                output_reader,
                invocation,
                parameter_snapshot,
                field="output_from",
                pointer=output_pointer,
            )
            if inspect.isawaitable(value):
                try:
                    value = await value
                except Exception:
                    raise ConfiguredSourceResolutionError("output_from", output_pointer) from None
            return {"output": value}

        post_resolver = resolve_output
    return pre_resolver, post_resolver


def _validate_checkpoint_payload_value(field: str, value: object, *, pointer: str) -> object:
    from ._observed_state import copy_observed_state

    if field == "context":
        if type(value) is not dict:
            raise ConfiguredSourceResolutionError(
                "context_from", pointer, reason="expected_context_object"
            ) from None
        reserved = {"norma_action", "norma_argument_shape", "state"}
        if reserved.intersection(value):
            raise ValueError("Checkpoint context_from cannot replace Norma-owned context keys")
    if field == "tools":
        if type(value) is not list or any(type(tool) is not dict for tool in value):
            raise ValueError("Checkpoint tools_from must resolve to a JSON array of objects")
    return copy_observed_state({"value": value})["value"]


async def _get_agent_session(plan: _AgentPlan, connection: Mapping[str, object]) -> None:
    runtime_url = resolve_url(cast(str | None, connection.get("runtime_url")))
    agent_key = resolve_environment("AGENT_KEY", cast(str | None, connection.get("agent_key")))
    target_type = cast(str | None, connection.get("target_type"))
    target_id = cast(str | None, connection.get("target_id"))
    control_contract = cast(ControlContract | None, connection.get("control_contract"))
    client_options: dict[str, Any] = {
        "runtime_url": runtime_url,
        "agent_key": agent_key,
        "control_contract": control_contract,
    }
    if cast(float, connection.get("timeout", 30.0)) != 30.0:
        client_options["timeout"] = connection["timeout"]
    if connection.get("transport") is not None:
        client_options["transport"] = connection["transport"]
    async with NormaClient(**client_options) as client:
        data = await get_agent(client, plan.name)
        agent_data = data.get("agent", data)
        if not isinstance(agent_data, dict) or agent_data.get("agent_name") != plan.name:
            raise ValueError(f"Runtime did not confirm existing Agent '{plan.name}'")
        agent = Agent.model_validate(agent_data)
        if agent.agent_name != plan.name:
            raise ValueError(f"Runtime returned a different Agent identity for '{plan.name}'")
        raw_steps = data.get("steps", [])
        if not isinstance(raw_steps, list):
            raise ValueError(f"Runtime returned invalid step schemas for Agent '{plan.name}'")
        try:
            plan.existing_schemas = [StepSchema.model_validate(step) for step in raw_steps]
        except (TypeError, ValueError):
            raise ValueError(
                f"Runtime returned invalid step schemas for Agent '{plan.name}'"
            ) from None
        response = await _list_agent_rules(
            client,
            plan.name,
            enabled_state="enabled",
            target_type=target_type,
            target_id=target_id,
        )
    from . import _rules_from_response

    rules = _rules_from_response(response)
    session = plan.state
    session.current_agent = agent
    session.configured_agent_name = plan.name
    session.server_url = runtime_url
    session.api_key = agent_key
    session.api_key_header = "X-API-Key"
    session.target_type = target_type
    session.target_id = target_id
    session.control_contract = control_contract
    session.client_timeout_seconds = cast(float, connection.get("timeout", 30.0))
    session.client_transport = cast(Any, connection.get("transport"))
    session.server_rules = rules
    session.rule_cache_healthy = True
    session.initialization_failed = False
    session.closed = False
    session.session_generation += 1
    session.refresh_interval_seconds = cast(int, connection.get("refresh_interval_seconds", 60))

    session.observability_settings = cast(Any, connection["validated_settings"])


def _validate_existing_step_schemas(plans: list[_AgentPlan]) -> None:
    """Check declared expectations and publication compatibility after every GET."""
    for plan in plans:
        existing = {(step.type, step.name): step for step in (plan.existing_schemas or [])}
        expected = list(plan.explicit_schemas)
        if plan.publish_schemas:
            expected.extend(plan.schemas)
        seen: set[tuple[str, str]] = set()
        for schema in expected:
            key = (schema.type, schema.name)
            if key in seen:
                continue
            seen.add(key)
            registered = existing.get(key)
            if registered is None:
                if not plan.publish_schemas:
                    raise ValueError(
                        f"Configured schema for Agent '{plan.name}' does not match existing steps: "
                        f"missing {key[0]}:{key[1]}"
                    )
                continue
            if (
                registered.input_schema != schema.input_schema
                or registered.output_schema != schema.output_schema
            ):
                raise ValueError(
                    f"Configured schema for Agent '{plan.name}' does not match existing "
                    f"{key[0]}:{key[1]} input/output schemas"
                )


async def _refresh_session(session: _StateContainer) -> list[dict[str, Any]]:
    if session.closed or session.current_agent is None:
        raise RuntimeError("Configured Agent session is closed")
    session.require_agent_identity()
    try:
        from .client import new_session_client

        async with new_session_client(NormaClient, session) as client:
            response = await _list_agent_rules(
                client,
                session.current_agent.agent_name,
                enabled_state="enabled",
                target_type=session.target_type,
                target_id=session.target_id,
            )
        from . import _rules_from_response

        rules = _rules_from_response(response)
    except Exception:
        with session.lifecycle_lock:
            session.rule_cache_healthy = False
        raise
    with session.lifecycle_lock:
        if not session.closed:
            session.server_rules = rules
            session.rule_cache_healthy = True
    return rules


def _run_sync(coroutine: Any) -> Any:
    from . import _run_coro_in_new_loop

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run_coro_in_new_loop(coroutine)
    result: list[Any] = []
    error: list[BaseException] = []
    context = copy_context()

    def target() -> None:
        try:
            result.append(context.run(_run_coro_in_new_loop, coroutine))
        except BaseException as exc:
            error.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0]


def _start_session_resources(plan: _AgentPlan, connection: Mapping[str, object]) -> None:
    from ._telemetry.sink_selection import (
        DEFAULT_RULE_EVENT_SINK_NAME,
        REGISTERED_RULE_EVENT_SINK_NAME,
        RuleEventSinkSelection,
    )
    from .observability import (
        EventBatcher,
        _BatcherRuleEventSink,
        _named_event_sink_factories,
        get_registered_rule_event_sinks,
    )

    session = plan.state
    with use_state(session):
        settings = session.observability_settings
        if settings is None:
            raise RuntimeError("Configured Agent observability settings are unavailable")
        if settings.observability_enabled:
            sink_name = settings.observability_sink_name
            if sink_name == DEFAULT_RULE_EVENT_SINK_NAME:
                batcher = EventBatcher(
                    runtime_url=session.server_url,
                    agent_key=session.api_key,
                    batch_size=cast(int, connection.get("batch_size", settings.batch_size)),
                    flush_interval=cast(
                        float, connection.get("flush_interval", settings.flush_interval)
                    ),
                )
                batcher.start()
                session.observability_batcher = batcher
                session.observability_sink = _BatcherRuleEventSink(batcher)
                session.observability_sink_owned = True
            elif sink_name == REGISTERED_RULE_EVENT_SINK_NAME:
                session.observability_sink = get_registered_rule_event_sinks()
            else:
                selection = RuleEventSinkSelection(
                    name=sink_name, config=settings.observability_sink_config
                )
                if sink_name == "otel":
                    from .otel_sink import create_otel_rule_event_sink

                    session.observability_sink = create_otel_rule_event_sink(
                        dict(selection.config),
                        tracer_provider=cast(Any, connection.get("otel_tracer_provider")),
                    )
                else:
                    session.observability_sink = _named_event_sink_factories.resolve(selection)
                session.observability_sink_owned = True
    if session.refresh_interval_seconds:
        stop = threading.Event()
        session.refresh_stop_event = stop
        worker = threading.Thread(
            target=_refresh_worker,
            args=(session, stop),
            name=f"norma-refresh-{plan.name}",
            daemon=True,
        )
        session.refresh_thread = worker
        worker.start()


def _refresh_worker(session: _StateContainer, stop: threading.Event) -> None:
    while not stop.wait(session.refresh_interval_seconds):
        try:
            with use_state(session):
                _run_sync(_refresh_session(session))
        except Exception:
            with session.lifecycle_lock:
                session.rule_cache_healthy = False


def _stop_session_refresh(session: _StateContainer) -> None:
    stop = session.refresh_stop_event
    thread = session.refresh_thread
    session.refresh_stop_event = None
    session.refresh_thread = None
    if stop is not None:
        stop.set()
    if thread is not None and thread.is_alive():
        thread.join(timeout=2)


def _deactivate_session(session: _StateContainer) -> None:
    with session.lifecycle_lock:
        if not session.closed:
            session.closed = True
            session.session_generation += 1
        session.rule_cache_healthy = False
        session.server_rules = None


def _close_session_resources(session: _StateContainer) -> DeliveryReceipt:
    _stop_session_refresh(session)
    from .observability import sync_shutdown_observability

    with use_state(session):
        return sync_shutdown_observability()


def _entrypoint_wrapper(
    original: Callable[..., Any],
    session: _StateContainer,
    spec: _EntryContextSpec | None = None,
) -> Callable[..., Any]:
    from .checkpoints import CheckError
    from .tracing import validate_span_id, validate_trace_id, with_trace

    def lifecycle_snapshot() -> tuple[int, Agent | None, str | None]:
        return session.session_generation, session.current_agent, session.configured_agent_name

    def ensure_invocation_live(snapshot: tuple[int, Agent | None, str | None]) -> None:
        generation, agent, expected_name = snapshot
        if (
            session.closed
            or session.session_generation != generation
            or session.current_agent is not agent
            or expected_name is None
            or agent is None
            or agent.agent_name != expected_name
        ):
            raise CheckError("Configured Agent session changed or closed; execution blocked.")

    def ensure_owned_slots() -> None:
        _ensure_owned_slots(session.instrumentation_slots)

    def needs_invocation() -> bool:
        return bool(spec and spec.requires_copy)

    async def resolve_scope(
        args: tuple[object, ...], kwargs: dict[str, object]
    ) -> tuple[
        dict[str, Any],
        str | None,
        str | None,
        tuple[str, str] | None,
        dict[str, object],
        dict[str, object] | None,
    ]:
        if needs_invocation():
            invocation_args, invocation_kwargs = _copy_entrypoint_arguments(original, args, kwargs)
        else:
            invocation_args, invocation_kwargs = args, kwargs
        invocation: dict[str, object] = {
            "args": invocation_args,
            "kwargs": invocation_kwargs,
            "result": None,
            "error": None,
            "context": None,
            "agent_name": session.configured_agent_name or "unknown",
        }
        try:
            parameter_snapshot = (
                spec.target_arguments.snapshot(invocation, _snapshot_entrypoint_value)
                if spec is not None
                and spec.requires_parameter_snapshot
                and spec.target_arguments is not None
                else None
            )
            context: object = {}
            if spec is not None and spec.context_reader is not None:
                context = resolve_value_source(spec.context_reader, invocation, parameter_snapshot)
                if inspect.isawaitable(context):
                    context = await context
            context = copy_observed_state(context)
            invocation["context"] = context
            target_type, target_id = session.target_type, session.target_id
            if spec is not None and spec.target_readers is not None:
                target_values: list[object] = []
                for reader in spec.target_readers:
                    value = resolve_value_source(reader, invocation, parameter_snapshot)
                    if inspect.isawaitable(value):
                        value = await value
                    target_values.append(value)
                if any(type(value) is not str or not value.strip() for value in target_values):
                    raise ValueError("Dynamic target scope must resolve to nonblank strings")
                target_type, target_id = cast(str, target_values[0]), cast(str, target_values[1])
            trace_ids = None
            if spec is not None and spec.trace_readers is not None:
                values: list[object] = []
                for reader in spec.trace_readers:
                    value = resolve_value_source(reader, invocation, parameter_snapshot)
                    if inspect.isawaitable(value):
                        value = await value
                    values.append(value)
                trace_id, span_id = values
                if (
                    type(trace_id) is not str
                    or type(span_id) is not str
                    or not validate_trace_id(trace_id)
                    or not validate_span_id(span_id)
                ):
                    raise ValueError("Configured trace identifiers are invalid")
                trace_ids = (trace_id, span_id)
            return context, target_type, target_id, trace_ids, invocation, parameter_snapshot
        except Exception:
            raise CheckError(
                "Configured entrypoint values could not be resolved; execution blocked."
            ) from None

    @contextmanager
    def activate(
        context: dict[str, Any],
        target_type: str | None,
        target_id: str | None,
        trace_ids: tuple[str, str] | None,
    ) -> Iterator[None]:
        with use_state(session):
            with use_invocation(session, target_type, target_id, context, trace_ids):
                if trace_ids is None:
                    yield
                else:
                    with with_trace(*trace_ids):
                        yield

    async def resolve_turn(
        invocation: dict[str, object],
        context: dict[str, Any],
        parameter_snapshot: dict[str, object] | None,
    ) -> tuple[Any, dict[str, Any] | None]:
        if spec is None or spec.turn is None:
            return None, None
        from .finish import TurnRecorder

        turn_spec = spec.turn
        try:
            values: dict[str, object] = {}
            for field in ("session_id", "turn_id", "user_request"):
                reader = turn_spec.get(f"{field}_reader")
                if reader is None:
                    continue
                value = resolve_value_source(reader, invocation, parameter_snapshot)
                if inspect.isawaitable(value):
                    value = await value
                values[field] = value
            if (
                type(values.get("session_id")) is not str
                or not cast(str, values["session_id"]).strip()
            ):
                raise ValueError("Turn session_id must resolve to a nonblank string")
            if type(values.get("turn_id")) is not str or not cast(str, values["turn_id"]).strip():
                raise ValueError("Turn turn_id must resolve to a nonblank string")
            if values.get("user_request") is not None and type(values["user_request"]) is not str:
                raise ValueError("Turn user_request must resolve to a string")
            recorder = TurnRecorder(
                session_id=cast(str, values["session_id"]),
                turn_id=cast(str, values["turn_id"]),
                user_request=cast(str | None, values.get("user_request")),
                platform=cast(str, turn_spec.get("platform", "python-sdk")),
                platform_version=cast(str | None, turn_spec.get("platform_version")),
                model=cast(str | None, turn_spec.get("model")),
                max_continuations=cast(int, turn_spec.get("max_continuations", 2)),
            )
            return recorder, cast(dict[str, Any] | None, turn_spec.get("finish"))
        except Exception:
            raise CheckError(
                "Configured turn values could not be resolved; execution blocked."
            ) from None

    async def finalize_turn(
        recorder: Any,
        finish: dict[str, Any] | None,
        invocation: dict[str, object],
        parameter_snapshot: dict[str, object] | None,
        result: Any,
        lifecycle: tuple[int, Agent | None, str | None],
    ) -> Any:
        if finish is None:
            return result
        if finish.get("driver", "generic") != "generic":
            raise CheckError("Configured native finish driver is not installed")
        from .finish import FinishHeldError, check_finish

        final_reader = cast(Callable[[dict[str, object]], object], finish["final_message_reader"])
        max_continuations = recorder.max_continuations
        current_result = result
        continuation_count = 0
        continuation_generation: str | None = None
        while True:
            ensure_invocation_live(lifecycle)
            finish_invocation = dict(invocation)
            finish_invocation["result"] = _snapshot_entrypoint_value(current_result)
            final_message = resolve_value_source(
                final_reader, finish_invocation, parameter_snapshot
            )
            if inspect.isawaitable(final_message):
                final_message = await final_message
            if type(final_message) is not str:
                raise CheckError("Configured finish message must resolve to a string")
            turn_record = recorder.turn(
                final_message,
                continuation_count=continuation_count,
                agent_generation=continuation_generation,
            )
            verdict = await check_finish(turn_record)
            callback_payload = {
                **finish_invocation,
                "result": current_result,
                "turn": turn_record,
                "verdict": verdict,
            }
            on_verdict = finish.get("on_verdict")
            if on_verdict is not None:
                callback_result = on_verdict(callback_payload)
                if inspect.isawaitable(callback_result):
                    await callback_result
            ensure_invocation_live(lifecycle)
            if verdict.verdict != "hold":
                return current_result
            continue_with = finish.get("continue_with")
            if continue_with is not None and continuation_count < max_continuations:
                continuation_count = verdict.continuation.count + 1
                continuation_generation = verdict.continuation.agent_generation
                continuation_payload = {
                    **callback_payload,
                    "continuation_count": continuation_count,
                }
                next_result = continue_with(continuation_payload)
                if inspect.isawaitable(next_result):
                    next_result = await next_result
                ensure_invocation_live(lifecycle)
                current_result = next_result
                continue
            if finish.get("on_hold") == "return":
                return current_result
            raise FinishHeldError(verdict, current_result)

    def make_native_finish_record(
        recorder: Any,
        finish: dict[str, Any],
        invocation: dict[str, object],
        scope: tuple[Any, ...],
    ) -> Any:
        from .finish import ConfiguredNativeFinish

        return ConfiguredNativeFinish(
            driver=cast(str, finish["driver"]),
            adapter_name=cast(str, finish["adapter"]),
            agent_name=session.configured_agent_name or "unknown",
            recorder=recorder,
            finish=finish,
            invocation=invocation,
            session=session,
            target_type=scope[1],
            target_id=scope[2],
        )

    def ensure_native_finish_used(record: Any) -> None:
        from .finish import configured_native_finish_was_used

        if not configured_native_finish_was_used(record):
            raise CheckError("Configured native finish did not match an eligible framework call.")

    def stream_hooks(
        recorder: Any,
        finish: dict[str, Any] | None,
        invocation: dict[str, object],
        parameter_snapshot: dict[str, object] | None,
        lifecycle: tuple[int, Agent | None, str | None],
    ) -> tuple[
        Callable[[Any], object] | None,
        Callable[[Any], object] | None,
        Callable[[object], object] | None,
        Callable[[object], object] | None,
    ]:
        if finish is None or "stream" not in finish:
            return None, None, None, None
        stream = cast(dict[str, Any], finish["stream"])
        aggregate = ""
        skip_text = object()
        reader = cast(Callable[[dict[str, object]], object] | None, stream.get("text_reader"))

        def resolve_text(chunk: Any) -> object:
            ensure_invocation_live(lifecycle)
            if reader is None:
                return chunk
            selected_invocation = dict(invocation)
            selected_invocation["result"] = _snapshot_entrypoint_value(chunk)
            try:
                return resolve_value_source(reader, selected_invocation, parameter_snapshot)
            except _MissingValuePath:
                if stream.get("text_is_path") and stream.get("missing") == "skip":
                    return skip_text
                raise CheckError(
                    "Configured streamed finish text path is unavailable; collection stopped."
                ) from None
            except Exception:
                raise CheckError(
                    "Configured streamed finish text selector failed; collection stopped."
                ) from None

        def append_text(selected: object) -> None:
            nonlocal aggregate
            ensure_invocation_live(lifecycle)
            if selected is skip_text or (selected is None and stream.get("missing") == "skip"):
                return
            if type(selected) is not str:
                raise CheckError(
                    "Configured streamed finish text must resolve to a string; collection stopped."
                )
            text = cast(str, selected)
            max_chars = cast(int, stream["max_chars"])
            if stream["mode"] == "last":
                if len(text) > max_chars:
                    raise CheckError(
                        "Configured streamed finish text exceeds max_chars; collection stopped."
                    )
                aggregate = text
            else:
                if len(text) > max_chars - len(aggregate):
                    raise CheckError(
                        "Configured streamed finish text exceeds max_chars; collection stopped."
                    )
                aggregate += text

        async def collect(chunk: Any) -> None:
            selected = resolve_text(chunk)
            if inspect.isawaitable(selected):
                try:
                    selected = await selected
                except _MissingValuePath:
                    if not stream.get("text_is_path"):
                        raise CheckError(
                            "Configured streamed finish text selector failed; collection stopped."
                        ) from None
                    if stream.get("missing") == "skip":
                        return
                    raise CheckError(
                        "Configured streamed finish text path is unavailable; collection stopped."
                    ) from None
                except Exception:
                    raise CheckError(
                        "Configured streamed finish text selector failed; collection stopped."
                    ) from None
            append_text(selected)

        def collect_sync(chunk: Any) -> object:
            selected = resolve_text(chunk)
            if inspect.isawaitable(selected):
                try:
                    selected = _run_sync(selected)
                except _MissingValuePath:
                    if not stream.get("text_is_path"):
                        raise CheckError(
                            "Configured streamed finish text selector failed; collection stopped."
                        ) from None
                    if stream.get("missing") == "skip":
                        return None
                    raise CheckError(
                        "Configured streamed finish text path is unavailable; collection stopped."
                    ) from None
                except Exception:
                    raise CheckError(
                        "Configured streamed finish text selector failed; collection stopped."
                    ) from None
            append_text(selected)
            return None

        async def finalize_stream(_return_value: object) -> None:
            ensure_invocation_live(lifecycle)
            from .finish import FinishHeldError, check_finish

            turn_record = recorder.turn(aggregate, continuation_count=0)
            verdict = await check_finish(turn_record)
            ensure_invocation_live(lifecycle)
            callback_payload = {
                **invocation,
                "result": aggregate,
                "turn": turn_record,
                "verdict": verdict,
            }
            on_verdict = finish.get("on_verdict")
            if on_verdict is not None:
                callback_result = on_verdict(callback_payload)
                if inspect.isawaitable(callback_result):
                    await callback_result
                ensure_invocation_live(lifecycle)
            if verdict.verdict == "hold" and finish.get("on_hold") == "raise":
                raise FinishHeldError(verdict, aggregate)

        def finalize_sync(return_value: object) -> object:
            return _run_sync(finalize_stream(return_value))

        return collect_sync, collect, finalize_sync, finalize_stream

    if inspect.isgeneratorfunction(original):
        from ._instrumentation_iterators import wrap_sync_iterator

        def sync_generator_entrypoint(*args: Any, **kwargs: Any) -> Any:
            iterator = original(*args, **kwargs)
            lifecycle = lifecycle_snapshot()
            scope: tuple[Any, ...] | None = None
            recorder: Any | None = None
            finish: dict[str, Any] | None = None
            hooks: tuple[Any, Any, Any, Any] | None = None

            @contextmanager
            def scope_factory(closing: bool) -> Iterator[None]:
                nonlocal scope, recorder, finish, hooks
                with use_state(session):
                    if closing:
                        if scope is None:
                            yield
                        elif recorder is None:
                            with activate(*scope[:4]):
                                yield
                        else:
                            with activate(*scope[:4]), recorder:
                                yield
                        return
                    if session.closed:
                        name = session.configured_agent_name or "unknown"
                        raise CheckError(f"Configured Agent '{name}' is closed.")
                    try:
                        session.require_agent_identity()
                    except RuntimeError as exc:
                        raise CheckError(str(exc)) from exc
                    ensure_owned_slots()
                    if scope is None:
                        scope = _run_sync(resolve_scope(args, kwargs))
                        recorder, finish = _run_sync(resolve_turn(scope[4], scope[0], scope[5]))
                    if hooks is None:
                        hooks = stream_hooks(recorder, finish, scope[4], scope[5], lifecycle)
                    ensure_invocation_live(lifecycle)
                    if recorder is None:
                        with activate(*scope[:4]):
                            yield
                    else:
                        with activate(*scope[:4]), recorder:
                            yield

            def collect_sync(chunk: Any) -> object:
                callback = hooks[0] if hooks is not None else None
                return callback(chunk) if callback is not None else None

            def finish_sync(return_value: object) -> object:
                callback = hooks[2] if hooks is not None else None
                return callback(return_value) if callback is not None else None

            has_stream_finish = bool(
                spec is not None
                and spec.turn is not None
                and isinstance(spec.turn.get("finish"), Mapping)
                and "stream" in spec.turn["finish"]
            )
            return wrap_sync_iterator(
                iterator,
                scope_factory,
                ensure_live=lambda: ensure_invocation_live(lifecycle),
                on_yield=collect_sync if has_stream_finish else None,
                on_finish=finish_sync if has_stream_finish else None,
            )

        functools_update_wrapper(sync_generator_entrypoint, original)
        return sync_generator_entrypoint

    if inspect.isasyncgenfunction(original):
        from ._instrumentation_iterators import wrap_async_iterator

        def async_generator_entrypoint(*args: Any, **kwargs: Any) -> Any:
            iterator = original(*args, **kwargs)
            lifecycle = lifecycle_snapshot()
            scope: tuple[Any, ...] | None = None
            recorder: Any | None = None
            finish: dict[str, Any] | None = None
            hooks: tuple[Any, Any, Any, Any] | None = None

            @asynccontextmanager
            async def scope_factory(closing: bool) -> Any:
                nonlocal scope, recorder, finish, hooks
                with use_state(session):
                    if closing:
                        if scope is None:
                            yield
                        elif recorder is None:
                            with activate(*scope[:4]):
                                yield
                        else:
                            with activate(*scope[:4]), recorder:
                                yield
                        return
                    if session.closed:
                        name = session.configured_agent_name or "unknown"
                        raise CheckError(f"Configured Agent '{name}' is closed.")
                    try:
                        session.require_agent_identity()
                    except RuntimeError as exc:
                        raise CheckError(str(exc)) from exc
                    ensure_owned_slots()
                    if scope is None:
                        scope = await resolve_scope(args, kwargs)
                        recorder, finish = await resolve_turn(scope[4], scope[0], scope[5])
                    if hooks is None:
                        hooks = stream_hooks(recorder, finish, scope[4], scope[5], lifecycle)
                    ensure_invocation_live(lifecycle)
                    if recorder is None:
                        with activate(*scope[:4]):
                            yield
                    else:
                        with activate(*scope[:4]), recorder:
                            yield

            async def collect_async(chunk: Any) -> object:
                callback = hooks[1] if hooks is not None else None
                if callback is None:
                    return None
                return await cast(Any, callback)(chunk)

            async def finish_async(return_value: object) -> object:
                callback = hooks[3] if hooks is not None else None
                if callback is None:
                    return None
                return await cast(Any, callback)(return_value)

            has_stream_finish = bool(
                spec is not None
                and spec.turn is not None
                and isinstance(spec.turn.get("finish"), Mapping)
                and "stream" in spec.turn["finish"]
            )
            return wrap_async_iterator(
                iterator,
                scope_factory,
                ensure_live=lambda: ensure_invocation_live(lifecycle),
                on_yield=collect_async if has_stream_finish else None,
                on_finish=finish_async if has_stream_finish else None,
            )

        functools_update_wrapper(async_generator_entrypoint, original)
        return async_generator_entrypoint

    if inspect.iscoroutinefunction(original):

        async def async_entrypoint(*args: Any, **kwargs: Any) -> Any:
            lifecycle = lifecycle_snapshot()
            with use_state(session):
                if session.closed:
                    name = session.current_agent.agent_name if session.current_agent else "unknown"
                    raise CheckError(f"Configured Agent '{name}' is closed.")
                try:
                    session.require_agent_identity()
                except RuntimeError as exc:
                    raise CheckError(str(exc)) from exc
                ensure_owned_slots()
                scope = await resolve_scope(args, kwargs)
                recorder, finish = await resolve_turn(scope[4], scope[0], scope[5])
                ensure_invocation_live(lifecycle)
            with activate(*scope[:4]):
                if recorder is None:
                    return await original(*args, **kwargs)
                with recorder:
                    if finish is not None and finish.get("driver") != "generic":
                        from .finish import use_configured_native_finish

                        record = make_native_finish_record(recorder, finish, scope[4], scope)
                        with use_configured_native_finish(record):
                            result = await original(*args, **kwargs)
                        ensure_native_finish_used(record)
                        return result
                    result = await original(*args, **kwargs)
                    return await finalize_turn(
                        recorder, finish, scope[4], scope[5], result, lifecycle
                    )

        functools_update_wrapper(async_entrypoint, original)
        return async_entrypoint

    def sync_entrypoint(*args: Any, **kwargs: Any) -> Any:
        lifecycle = lifecycle_snapshot()
        with use_state(session):
            if session.closed:
                name = session.current_agent.agent_name if session.current_agent else "unknown"
                raise CheckError(f"Configured Agent '{name}' is closed.")
            try:
                session.require_agent_identity()
            except RuntimeError as exc:
                raise CheckError(str(exc)) from exc
            ensure_owned_slots()
            scope = _run_sync(resolve_scope(args, kwargs))
            recorder, finish = _run_sync(resolve_turn(scope[4], scope[0], scope[5]))
            ensure_invocation_live(lifecycle)
            with activate(*scope[:4]):
                if recorder is None:
                    result = original(*args, **kwargs)
                else:
                    with recorder:
                        if finish is not None and finish.get("driver") != "generic":
                            from .finish import use_configured_native_finish

                            record = make_native_finish_record(recorder, finish, scope[4], scope)
                            with use_configured_native_finish(record):
                                result = original(*args, **kwargs)
                            if not inspect.isawaitable(result):
                                ensure_native_finish_used(record)
                                return result
                        else:
                            record = None
                            result = original(*args, **kwargs)
                            if not inspect.isawaitable(result):
                                return _run_sync(
                                    finalize_turn(
                                        recorder, finish, scope[4], scope[5], result, lifecycle
                                    )
                                )
        if inspect.isawaitable(result):

            async def await_scoped() -> Any:
                with activate(*scope[:4]):
                    if recorder is None:
                        return await result
                    with recorder:
                        if finish is not None and finish.get("driver") != "generic":
                            from .finish import use_configured_native_finish

                            assert record is not None
                            with use_configured_native_finish(record):
                                final_result = await result
                            ensure_native_finish_used(record)
                            return final_result
                        final_result = await result
                        return await finalize_turn(
                            recorder, finish, scope[4], scope[5], final_result, lifecycle
                        )

            return await_scoped()
        return result

    functools_update_wrapper(sync_entrypoint, original)
    return sync_entrypoint


def _copy_entrypoint_arguments(
    original: Callable[..., Any], args: tuple[object, ...], kwargs: dict[str, object]
) -> tuple[tuple[object, ...], dict[str, object]]:
    del original
    return (
        cast(tuple[object, ...], _snapshot_entrypoint_value(args)),
        cast(dict[str, object], _snapshot_entrypoint_value(kwargs)),
    )


def _snapshot_entrypoint_value(value: object, memo: dict[int, object] | None = None) -> object:
    """Copy ordinary containers without invoking copy hooks on native objects."""
    memo = {} if memo is None else memo
    if value is None or type(value) in {str, int, float, bool, bytes}:
        return value
    identity = id(value)
    if identity in memo:
        return memo[identity]
    if type(value) is list:
        result: list[object] = []
        memo[identity] = result
        result.extend(_snapshot_entrypoint_value(item, memo) for item in value)
        return result
    if type(value) is tuple:
        # Tuples are JSON-like immutable containers; a temporary list handles
        # recursive references without invoking a user-defined copy protocol.
        holder: list[object] = []
        memo[identity] = holder
        holder.extend(_snapshot_entrypoint_value(item, memo) for item in value)
        tuple_value = tuple(holder)
        memo[identity] = tuple_value
        return tuple_value
    if type(value) is dict:
        result_dict: dict[object, object] = {}
        memo[identity] = result_dict
        for key, item in value.items():
            result_dict[_snapshot_entrypoint_value(key, memo)] = _snapshot_entrypoint_value(
                item, memo
            )
        return result_dict
    return value


def _prepare_observability_and_wrappers(
    plans: list[_AgentPlan],
    connections: Mapping[str, Mapping[str, object]],
) -> tuple[list[_Slot], dict[str, list[StepSchema]]]:
    from . import _rule_registry

    before_registry = dict(_rule_registry._registered_steps)
    _revalidate_slots(plans)
    created_registry: dict[tuple[str, str], object] = {}
    wrapper_slots: list[_Slot] = []
    for plan in plans:
        plan.state.instrumentation_slots.clear()
    checkpoint_entries: dict[
        tuple[int, str],
        list[tuple[_AgentPlan, str, dict[str, object], _Resolved]],
    ] = {}
    entrypoint_entries: dict[
        tuple[int, str], tuple[_AgentPlan, _Resolved, _EntryContextSpec | None]
    ] = {}
    operation_entries: list[tuple[_AgentPlan, Any]] = []

    def make_checkpoint(
        plan: _AgentPlan,
        step_name: str,
        item: dict[str, object],
        resolved: _Resolved,
        target: Callable[..., Any],
    ) -> Callable[..., Any]:
        options_obj = cast(dict[str, object], item.get("native_options", {}))
        source_paths = (
            {"state": _config_pointer(("checkpoints", step_name, "state_provider"))}
            if "state_provider" in item
            else None
        )
        built = checkpoint(
            step_name=step_name,
            step_type=cast(str, item.get("step_type", "tool")),
            _instrumentation_resolver=cast(Any, item.get("instrumentation_resolver")),
            _instrumentation_post_resolver=cast(Any, item.get("instrumentation_post_resolver")),
            _instrumentation_source_paths=source_paths,
            _instrumentation_input_schema=item.get("instrumentation_input_schema"),
            _instrumentation_target_arguments=cast(
                Any, item.get("instrumentation_target_arguments")
            ),
            **cast(Any, options_obj),
        )(target)
        registry_key = (cast(str, item.get("step_type", "tool")), step_name)
        registered = _rule_registry._registered_steps.get(registry_key)
        if registered is not None:
            created_registry[registry_key] = registered
        setattr(built, _MARKER, True)
        return built

    try:
        for plan in plans:
            for identity, (step_name, item, resolved) in plan.checkpoints.items():
                checkpoint_entries.setdefault(identity, []).append(
                    (plan, step_name, item, resolved)
                )
            for resolved in plan.entrypoints:
                entrypoint_entries[resolved.identity] = (
                    plan,
                    resolved,
                    (plan.entrypoint_contexts or {}).get(resolved.identity),
                )
            operation_entries.extend(
                (plan, operation) for operation in (plan.operations or {}).values()
            )

        identities = set(checkpoint_entries) | set(entrypoint_entries)
        for identity in identities:
            checkpoint_specs = checkpoint_entries.get(identity, [])
            entrypoint = entrypoint_entries.get(identity)
            resolved = entrypoint[1] if entrypoint is not None else checkpoint_specs[0][3]
            handlers: dict[int, Callable[..., Any]] = {}
            fallback: Callable[..., Any] | None = None
            if entrypoint is not None:
                entry_plan, _, context_spec = entrypoint
                own_specs = [spec for spec in checkpoint_specs if spec[0] is entry_plan]
                if len(own_specs) > 1:
                    raise ValueError(
                        f"Duplicate same-agent checkpoint for entrypoint '{resolved.path}'"
                    )
                inner = resolved.raw
                if own_specs:
                    _, step_name, item, checkpoint_target = own_specs[0]
                    inner = make_checkpoint(entry_plan, step_name, item, checkpoint_target, inner)
                entry = _entrypoint_wrapper(inner, entry_plan.state, context_spec)
                setattr(entry, _MARKER, True)
                fallback = entry
                handlers[id(entry_plan.state)] = entry
                for checkpoint_plan, step_name, item, checkpoint_target in checkpoint_specs:
                    if checkpoint_plan is entry_plan:
                        continue
                    handlers[id(checkpoint_plan.state)] = make_checkpoint(
                        checkpoint_plan, step_name, item, checkpoint_target, entry
                    )
            else:
                for checkpoint_plan, step_name, item, checkpoint_target in checkpoint_specs:
                    handlers[id(checkpoint_plan.state)] = make_checkpoint(
                        checkpoint_plan, step_name, item, checkpoint_target, resolved.raw
                    )

            if entrypoint is not None and len(handlers) == 1 and not checkpoint_specs:
                installed = _install(resolved, fallback or resolved.raw)
            else:
                is_async = inspect.iscoroutinefunction(resolved.raw)

                def selected(
                    mapping: dict[int, Callable[..., Any]],
                    entry_fallback: Callable[..., Any] | None,
                    async_target: bool,
                ) -> Callable[..., Any]:
                    async def async_dispatch(*args: Any, **kwargs: Any) -> Any:
                        handler = _authorized_checkpoint(mapping, entry_fallback)
                        value = handler(*args, **kwargs)
                        if inspect.isawaitable(value):
                            return await value
                        return value

                    def sync_dispatch(*args: Any, **kwargs: Any) -> Any:
                        return _authorized_checkpoint(mapping, entry_fallback)(*args, **kwargs)

                    return async_dispatch if async_target else sync_dispatch

                dispatcher = selected(handlers, fallback, is_async)
                functools_update_wrapper(dispatcher, resolved.raw)
                setattr(dispatcher, _MARKER, True)
                installed = _install(resolved, dispatcher)
            slot = _Slot(
                resolved.owner,
                resolved.attribute,
                resolved.previous,
                installed,
                resolved.was_present,
                resolved.path,
            )
            wrapper_slots.append(slot)
            owning_plans = ([entrypoint[0]] if entrypoint is not None else []) + [
                spec[0] for spec in checkpoint_specs
            ]
            for owner_plan in {id(plan): plan for plan in owning_plans}.values():
                owner_plan.state.instrumentation_slots.append(slot)
        from ._instrumentation_operations import operation_wrapper

        for plan, operation in operation_entries:
            wrapped = operation_wrapper(operation, operation.target.raw, plan.state)
            setattr(wrapped, _MARKER, True)
            installed = _install(operation.target, wrapped)
            slot = _Slot(
                operation.target.owner,
                operation.target.attribute,
                operation.target.previous,
                installed,
                operation.target.was_present,
                operation.target.path,
            )
            wrapper_slots.append(slot)
            plan.state.instrumentation_slots.append(slot)
        _restore_created_registry(before_registry, created_registry, _rule_registry)
    except Exception:
        for slot in reversed(wrapper_slots):
            _restore(slot)
        for plan in plans:
            plan.state.instrumentation_slots.clear()
        _restore_created_registry(before_registry, created_registry, _rule_registry)
        raise
    return wrapper_slots, {plan.name: plan.schemas for plan in plans}


def _authorized_checkpoint(
    mapping: Mapping[int, Callable[..., Any]],
    entrypoint_fallback: Callable[..., Any] | None = None,
) -> Callable[..., Any]:
    """Select a wrapper only for the active, identity-valid configured session."""
    from ._state import active_state
    from .checkpoints import CheckError

    session = active_state()
    if session is None and entrypoint_fallback is not None:
        return entrypoint_fallback
    if session is None or session.closed:
        raise CheckError("A configured entrypoint scope is required for this checkpoint.")
    try:
        session.require_agent_identity()
    except RuntimeError as exc:
        raise CheckError(str(exc)) from exc
    _ensure_owned_slots(session.instrumentation_slots)
    handler = mapping.get(id(session))
    if handler is None:
        agent = session.current_agent.agent_name if session.current_agent else "unknown"
        raise CheckError(f"Checkpoint is not configured for Agent '{agent}'.")
    return handler


def functools_update_wrapper(wrapper: Callable[..., Any], wrapped: Callable[..., Any]) -> None:
    import functools

    functools.update_wrapper(wrapper, wrapped)


def _restore_created_registry(
    before: Mapping[tuple[str, str], object],
    created: Mapping[tuple[str, str], object],
    registry: Any,
) -> None:
    for key, added in created.items():
        if registry._registered_steps.get(key) is not added:
            continue
        previous = before.get(key, _MISSING)
        if previous is _MISSING:
            registry._registered_steps.pop(key, None)
        else:
            registry._registered_steps[key] = previous


def _revalidate_slots(plans: list[_AgentPlan]) -> None:
    targets: dict[tuple[int, str], _Resolved] = {}
    for plan in plans:
        for _, _, resolved in plan.checkpoints.values():
            targets[resolved.identity] = resolved
        for operation in (plan.operations or {}).values():
            targets[operation.target.identity] = operation.target
        for resolved in plan.entrypoints:
            targets[resolved.identity] = resolved
        for adapter in plan.framework_adapters or []:
            adapter.revalidate()
    for resolved in targets.values():
        try:
            current = _read_slot(resolved.owner, resolved.attribute)
        except (AttributeError, KeyError):
            raise ValueError(f"Target '{resolved.path}' changed during configuration") from None
        if current is not resolved.previous:
            raise ValueError(f"Target '{resolved.path}' changed during configuration")


async def aconfigure(
    source: str | os.PathLike[str] | Mapping[str, object],
    *,
    agent_name: str | None = None,
    bindings: Mapping[str, object],
    providers: Mapping[str, Callable[..., object]] | None = None,
    options: Mapping[str, Mapping[str, object]] | None = None,
    connection: Mapping[str, object] | None = None,
    connections: Mapping[str, Mapping[str, object]] | None = None,
) -> ConfiguredAgents:
    """Bind existing Agents from a mapping, config file, or flat config directory.

    File stems are exact Agent identities. A mapping requires ``agent_name``.
    This authenticates and reads each existing Agent and its enabled Rules; it
    does not create Agent identities or write schemas.
    """
    configs = _parse_source(source, agent_name)
    providers, options = providers or {}, options or {}
    names = set(configs)
    resolved_connections = {
        name: _connection_settings(name, configs[name], bindings, connection, connections, names)
        for name in configs
    }
    logging_configs = [
        cast(dict[str, Any], resolved["_logging_config"])
        for resolved in resolved_connections.values()
        if resolved.get("_logging_config") is not None
    ]
    if logging_configs and any(value != logging_configs[0] for value in logging_configs[1:]):
        raise ValueError("All configured Agents must use the same process logging config")
    plans = [
        _build_plan(name, config, bindings, providers, options) for name, config in configs.items()
    ]
    checkpoint_owners: dict[tuple[int, str], set[str]] = {}
    operation_owners: dict[tuple[int, str], str] = {}
    entrypoint_owner: dict[tuple[int, str], str] = {}
    for plan in plans:
        for identity in plan.checkpoints:
            checkpoint_owners.setdefault(identity, set()).add(plan.name)
        for identity in plan.operations or {}:
            if identity in checkpoint_owners or identity in operation_owners:
                raise ValueError("Operation target cannot also be a checkpoint target")
            operation_owners[identity] = plan.name
        for resolved in plan.entrypoints:
            if resolved.identity in entrypoint_owner:
                raise ValueError(f"Entrypoint target belongs to multiple agents: {resolved.path}")
            entrypoint_owner[resolved.identity] = plan.name
    if set(operation_owners) & set(checkpoint_owners):
        raise ValueError("Operation target cannot also be a checkpoint target")
    if set(operation_owners) & set(entrypoint_owner):
        raise ValueError("A target cannot be both an operation and entrypoint")
    for identity in set(checkpoint_owners) & set(entrypoint_owner):
        participants = checkpoint_owners[identity] | {entrypoint_owner[identity]}
        if any(configs[name]["version"] != 2 for name in participants):
            raise ValueError("A target cannot be both a v1 checkpoint and entrypoint")
    framework_descriptors = [adapter for plan in plans for adapter in plan.framework_adapters or []]
    if framework_descriptors:
        from ._instrumentation_framework_adapters import validate_framework_adapter_batch

        validate_framework_adapter_batch(framework_descriptors)
    native_descriptors = [adapter for plan in plans for adapter in plan.native_adapters or []]
    if native_descriptors:
        from ._instrumentation_native_finish import validate_native_adapter_batch

        validate_native_adapter_batch(native_descriptors)
    # No remote operation or mutation occurs until every config and binding validates.
    for plan in plans:
        await _get_agent_session(plan, resolved_connections[plan.name])
    _validate_existing_step_schemas(plans)
    _revalidate_slots(plans)
    native_adapters: Any | None = None
    if native_descriptors:
        from ._instrumentation_native_finish import prepare_native_adapters

        native_adapters = await prepare_native_adapters(
            native_descriptors,
            {plan.name: plan.state for plan in plans},
        )
    published_names: list[str] = []
    try:
        if native_adapters is not None:
            for plan in plans:
                merged = {(step.type, step.name): step for step in plan.schemas}
                for step in native_adapters.schemas.get(plan.name, []):
                    key = (step.type, step.name)
                    existing = merged.get(key)
                    if existing is not None and existing != step:
                        raise ValueError(
                            f"Prepared native schema conflicts for Agent '{plan.name}', "
                            f"{key[0]}:{key[1]}"
                        )
                    merged.setdefault(key, step)
                plan.schemas = list(merged.values())
        _validate_existing_step_schemas(plans)
        for plan in plans:
            if not plan.publish_schemas:
                continue
            from .client import new_session_client

            try:
                async with new_session_client(NormaClient, plan.state) as client:
                    response = await client.put_agent_steps(
                        plan.name,
                        [step.model_dump(mode="json", exclude_unset=True) for step in plan.schemas],
                    )
                plan.schemas = [StepSchema.model_validate(step) for step in response["steps"]]
                published_names.append(plan.name)
            except Exception:
                published_label = ", ".join(published_names) if published_names else "none"
                raise RuntimeError(
                    f"Step schema publication failed for Agent '{plan.name}'; "
                    f"previously published Agents: {published_label}"
                ) from None
        _revalidate_slots(plans)
    except Exception:
        if native_adapters is not None:
            await native_adapters.aclose()
        raise
    slots: list[_Slot] = []
    framework_adapters = [adapter for plan in plans for adapter in plan.framework_adapters or []]
    logging_snapshot = None
    if logging_configs:
        from .observability import configure_logging
        from .settings import configure_settings, get_settings

        logging_snapshot = get_settings().model_copy(deep=True)
    try:
        if logging_configs:
            configure_logging(logging_configs[0])
        for plan in plans:
            _start_session_resources(plan, resolved_connections[plan.name])
        slots, schemas = _prepare_observability_and_wrappers(plans, resolved_connections)
        for adapter in framework_adapters:
            plan = next(plan for plan in plans if plan.name == adapter.agent_name)
            adapter.install(plan.state)
        if native_adapters is not None:
            native_adapters.install()
    except Exception:
        for adapter in reversed(framework_adapters):
            try:
                await adapter.aclose()
            except Exception:
                pass
        if native_adapters is not None:
            try:
                await native_adapters.aclose()
            except Exception:
                pass
        for plan in plans:
            _deactivate_session(plan.state)
        for plan in plans:
            _close_session_resources(plan.state)
        for slot in reversed(slots):
            _restore(slot)
        for plan in plans:
            plan.state.instrumentation_slots.clear()
        if logging_snapshot is not None:
            from .observability import configure_logging
            from .settings import configure_settings

            configure_settings(**logging_snapshot.model_dump())
            configure_logging(
                {
                    "enabled": logging_snapshot.log_enabled,
                    "span_start": logging_snapshot.log_span_start,
                    "span_end": logging_snapshot.log_span_end,
                    "rule_eval": logging_snapshot.log_rule_eval,
                }
            )
        raise
    return ConfiguredAgents(
        {plan.name: plan.state for plan in plans},
        schemas,
        slots,
        framework_adapters,
        native_adapters,
    )


def configure(
    source: str | os.PathLike[str] | Mapping[str, object],
    *,
    agent_name: str | None = None,
    bindings: Mapping[str, object],
    providers: Mapping[str, Callable[..., object]] | None = None,
    options: Mapping[str, Mapping[str, object]] | None = None,
    connection: Mapping[str, object] | None = None,
    connections: Mapping[str, Mapping[str, object]] | None = None,
) -> ConfiguredAgents:
    """Synchronously bind existing Agents; see :func:`aconfigure` for semantics."""
    return _run_sync(
        aconfigure(
            source,
            agent_name=agent_name,
            bindings=bindings,
            providers=providers,
            options=options,
            connection=connection,
            connections=connections,
        )
    )
