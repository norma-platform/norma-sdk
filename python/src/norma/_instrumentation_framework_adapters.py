# Modified for Norma.
"""Configured Google ADK and Strands adapter lifecycle support."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from ._instrumentation_values import resolve_binding
from ._state import _StateContainer, use_state


@dataclass
class FrameworkAdapter:
    name: str
    agent_name: str
    kind: str
    agent: object
    plugin: object
    schemas: list[dict[str, Any]]
    runner: object | None = None
    manager: object | None = None
    event_registry: object | None = None
    event_snapshot: dict[object, list[object]] | None = None
    plugin_registry: list[object] | None = None
    installed: bool = False

    def revalidate(self) -> None:
        if self.kind == "google_adk":
            assert self.runner is not None and self.manager is not None
            if (
                _ordinary_attribute(
                    self.runner, "plugin_manager", "Google ADK Runner plugin manager"
                )
                is not self.manager
            ):
                raise ValueError("Google ADK adapter target changed during configuration")
            if (
                _ordinary_attribute(self.manager, "plugins", "Google ADK plugin registry")
                is not self.plugin_registry
            ):
                raise ValueError("Google ADK plugin registry changed during configuration")
            if (
                _ordinary_attribute(self.runner, "agent", "Google ADK Runner agent")
                is not self.agent
            ):
                raise ValueError("Google ADK Runner agent changed during configuration")
            return
        if self.kind == "strands":
            if (
                _ordinary_attribute(self.agent, "hooks", "Strands Agent hook registry")
                is not self.event_registry
            ):
                raise ValueError("Strands Agent hook registry changed during configuration")

    def install(self, session: _StateContainer) -> None:
        if self.installed:
            return
        self.revalidate()
        if self.kind == "google_adk":
            assert self.runner is not None and self.manager is not None
            with use_state(session):
                cast(Any, self.plugin).bind(self.agent)
            plugins = self.plugin_registry
            if type(plugins) is not list:
                raise ValueError("Google ADK plugin manager has an unsupported plugin registry")
            if any(item is self.plugin for item in plugins):
                raise ValueError("Google ADK plugin is already registered on this Runner")
            if any(
                getattr(item, "name", None) == getattr(self.plugin, "name", None)
                for item in plugins
            ):
                raise ValueError("Google ADK Runner already has a Norma plugin")
            register = inspect.getattr_static(type(self.manager), "register_plugin", None)
            if register is None or not callable(getattr(self.manager, "register_plugin", None)):
                raise ValueError("Google ADK plugin manager cannot register plugins")
            try:
                cast(Any, self.manager).register_plugin(self.plugin)
            except Exception:
                self._remove_google_plugin()
                raise
            self.installed = True
            return

        if self.kind == "strands":
            registry = self.event_registry
            snapshot = self.event_snapshot
            if registry is None or snapshot is None:
                raise ValueError("Strands hook registry was not validated")
            callbacks = vars(registry).get("_registered_callbacks")
            if type(callbacks) is not dict:
                raise ValueError("Strands hook registry has an unsupported callback layout")
            self.installed = True
            try:
                cast(Any, self.plugin).init_agent(self.agent)
            except Exception:
                self._remove_strands_hooks()
                self.installed = False
                raise
            return
        raise ValueError(f"Unsupported configured framework adapter '{self.kind}'")

    def close(self) -> None:
        if self.kind == "google_adk" and self.installed:
            self._remove_google_plugin()
        elif self.kind == "strands" and self.installed:
            self._remove_strands_hooks()
        self.installed = False

    async def aclose(self) -> None:
        self.close()
        if self.kind == "google_adk":
            close = getattr(self.plugin, "close", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result

    def _remove_google_plugin(self) -> None:
        assert self.manager is not None
        plugins = vars(self.manager).get("plugins")
        if type(plugins) is list:
            plugins[:] = [plugin for plugin in plugins if plugin is not self.plugin]

    def _remove_strands_hooks(self) -> None:
        registry = self.event_registry
        if registry is None:
            return
        callbacks = vars(registry).get("_registered_callbacks")
        if type(callbacks) is not dict:
            return
        snapshot = self.event_snapshot or {}
        for event_type, entries in list(callbacks.items()):
            if type(entries) is not list:
                continue
            retained = [
                entry
                for entry in entries
                if not (
                    inspect.ismethod(getattr(entry, "callback", None))
                    and getattr(entry, "callback").__self__ is self.plugin
                )
            ]
            if retained or event_type in snapshot:
                callbacks[event_type] = retained
            else:
                callbacks.pop(event_type, None)


def compile_framework_adapters(
    name: str,
    config: Mapping[str, Any],
    bindings: Mapping[str, object],
) -> tuple[list[FrameworkAdapter], list[dict[str, Any]]]:
    descriptors: list[FrameworkAdapter] = []
    schemas: list[dict[str, Any]] = []
    for adapter_name, raw_adapter in config.get("adapters", {}).items():
        adapter = dict(raw_adapter)
        kind = adapter["kind"]
        if kind == "google_adk":
            descriptor, discovered = _compile_google_adapter(name, adapter_name, adapter, bindings)
        elif kind == "strands":
            descriptor, discovered = _compile_strands_adapter(name, adapter_name, adapter, bindings)
        else:
            continue
        descriptors.append(descriptor)
        schemas.extend(discovered)
    return descriptors, schemas


def validate_framework_adapter_batch(descriptors: list[FrameworkAdapter]) -> None:
    """Reject a native framework object assigned to multiple Norma identities."""
    owners: dict[tuple[str, int], FrameworkAdapter] = {}
    for descriptor in descriptors:
        native_object = descriptor.runner if descriptor.kind == "google_adk" else descriptor.agent
        key = (descriptor.kind, id(native_object))
        previous = owners.get(key)
        if previous is not None and previous.agent_name != descriptor.agent_name:
            raise ValueError(
                f"One {descriptor.kind} native object cannot be configured "
                "for multiple Norma Agents"
            )
        if previous is not None:
            raise ValueError(f"Native object is already assigned to adapter '{previous.name}'")
        owners[key] = descriptor


def _compile_google_adapter(
    agent_name: str,
    adapter_name: str,
    config: dict[str, Any],
    bindings: Mapping[str, object],
) -> tuple[FrameworkAdapter, list[dict[str, Any]]]:
    try:
        from norma.integrations.google_adk.plugin import NormaPlugin
    except (ImportError, RuntimeError) as exc:
        raise ValueError(str(exc)) from None
    runner = resolve_binding(config["runner"]["binding"], bindings)
    if "agent" in config:
        agent = resolve_binding(config["agent"]["binding"], bindings)
    else:
        agent = _ordinary_attribute(runner, "agent", "Google ADK Runner agent")
    manager = _ordinary_attribute(runner, "plugin_manager", "Google ADK Runner plugin manager")
    plugins = _ordinary_attribute(manager, "plugins", "Google ADK plugin registry")
    if type(plugins) is not list:
        raise ValueError("Google ADK plugin registry must be a list")
    register = getattr(manager, "register_plugin", None)
    if not callable(register):
        raise ValueError("Google ADK plugin manager cannot register plugins")
    if any(getattr(plugin, "name", None) == NormaPlugin.name for plugin in plugins):
        raise ValueError("Google ADK Runner already has a Norma plugin")
    callbacks = {
        key: _resolve_callback(config[key], bindings, f"adapters.{adapter_name}.{key}")
        for key in (
            "step_name_resolver",
            "context_extractor",
            "on_violation_callback",
        )
        if key in config
    }
    enabled = set(config.get("enabled_hooks", [])) or None
    # Construct against an isolated empty state: compiling a config must not depend
    # on the legacy singleton's currently selected Agent.
    with use_state(_StateContainer()):
        plugin = NormaPlugin(
            agent_name,
            enabled_hooks=enabled,
            blocked_message_template=config.get("blocked_message_template"),
            step_name_overrides=config.get("step_name_overrides"),
            enable_logging=config.get("enable_logging", True),
            register_schemas=False,
            **callbacks,
        )
    steps = plugin._discover_steps(agent)
    descriptor = FrameworkAdapter(
        adapter_name,
        agent_name,
        "google_adk",
        agent,
        plugin,
        [dict(step) for step in steps],
        runner=runner,
        manager=manager,
        plugin_registry=plugins,
    )
    return descriptor, [dict(step) for step in steps]


def _compile_strands_adapter(
    agent_name: str,
    adapter_name: str,
    config: dict[str, Any],
    bindings: Mapping[str, object],
) -> tuple[FrameworkAdapter, list[dict[str, Any]]]:
    try:
        from strands.hooks.registry import HookRegistry

        from norma.integrations.strands.plugin import NormaPlugin
    except Exception:
        raise ValueError(
            "Strands instrumentation requires the installed strands-agents dependency"
        ) from None
    agent = resolve_binding(config["agent"]["binding"], bindings)
    registry = _ordinary_attribute(agent, "hooks", "Strands Agent hook registry")
    if type(registry) is not HookRegistry:
        raise ValueError("Strands Agent uses an unsupported hook registry")
    callbacks = vars(registry).get("_registered_callbacks")
    if type(callbacks) is not dict or any(
        type(entries) is not list
        or any(not hasattr(entry, "callback") or not hasattr(entry, "order") for entry in entries)
        for entries in callbacks.values()
    ):
        raise ValueError("Strands hook registry has an unsupported callback layout")
    snapshot = {event_type: list(entries) for event_type, entries in callbacks.items()}
    hook_module = __import__("strands.hooks", fromlist=["hooks"])
    event_types = None
    if "event_types" in config:
        event_types = [getattr(hook_module, event_type) for event_type in config["event_types"]]
    callback = (
        _resolve_callback(
            config["on_violation_callback"],
            bindings,
            f"adapters.{adapter_name}.on_violation_callback",
        )
        if "on_violation_callback" in config
        else None
    )
    plugin = NormaPlugin(
        agent_name,
        event_types=event_types,
        on_violation_callback=callback,
        enable_logging=config.get("enable_logging", True),
    )
    descriptor = FrameworkAdapter(
        adapter_name,
        agent_name,
        "strands",
        agent,
        plugin,
        [],
        event_registry=registry,
        event_snapshot=snapshot,
    )
    return descriptor, []


def _resolve_callback(
    reference: Mapping[str, object], bindings: Mapping[str, object], field: str
) -> Callable[..., Any]:
    callback = resolve_binding(cast(str, reference["binding"]), bindings)
    if not callable(callback):
        raise ValueError(f"{field} binding must be a function")
    return cast(Callable[..., Any], callback)


def _ordinary_attribute(owner: object, name: str, description: str) -> object:
    if type(owner).__getattribute__ is not object.__getattribute__:
        raise ValueError(f"{description} uses custom attribute access")
    values = object.__getattribute__(owner, "__dict__")
    if type(values) is not dict or name not in values:
        raise ValueError(f"{description} is missing")
    return values[name]
