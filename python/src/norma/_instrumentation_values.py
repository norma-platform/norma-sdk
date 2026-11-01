# Modified for Norma.
"""Safe value-source resolution for versioned instrumentation configuration."""

from __future__ import annotations

import copy
import inspect
import types
from collections.abc import Callable, Mapping
from typing import Any, Literal, cast

_VALUE_ROOTS = {"args", "kwargs", "result", "error", "context"}
_RECEIVER_MODES = {"class-function", "classmethod", "instance-method", "instance-class"}


class _MissingValuePath(ValueError):
    """A safe selector path is absent, rather than failing inside user code."""


class UnknownTargetParameterError(ValueError):
    """A named selector refers to a parameter absent from its target signature."""

    def __init__(self, parameter_name: str) -> None:
        self.parameter_name = parameter_name
        super().__init__(
            f"Instrumentation params path names no target parameter '{parameter_name}'"
        )


class ConfiguredSourceResolutionError(ValueError):
    """A configured checkpoint ValueSource failed without retaining its raw error."""

    def __init__(
        self,
        field: str,
        pointer: str,
        *,
        reason: Literal["expected_context_object"] | None = None,
    ) -> None:
        self.field = field
        self.pointer = pointer
        self.reason = reason
        super().__init__(field)


class _TargetArguments:
    """Resolve named user parameters from one configured callable's signature."""

    def __init__(self, target: Callable[..., object], *, skip_receiver: bool) -> None:
        self._target = target
        self._skip_receiver = skip_receiver
        self._signature: inspect.Signature | None = None
        self._user_parameter_names: tuple[str, ...] = ()
        self._receiver_name: str | None = None
        self._variadic_receiver_name: str | None = None

    def prepare(self) -> None:
        if self._signature is not None:
            return
        try:
            signature = inspect.signature(self._target)
        except Exception:
            raise ValueError(
                "Instrumentation input/params selectors require an inspectable target signature"
            ) from None
        names = tuple(signature.parameters)
        if self._skip_receiver and names:
            first_parameter = next(iter(signature.parameters.values()))
            if first_parameter.kind is inspect.Parameter.VAR_POSITIONAL:
                # A method without an explicit receiver parameter receives its
                # instance in the first item of its *args tuple.
                self._variadic_receiver_name = first_parameter.name
            else:
                self._receiver_name = first_parameter.name
                names = names[1:]
        self._signature = signature
        self._user_parameter_names = names

    @property
    def application_signature(self) -> inspect.Signature:
        """Signature of user arguments after applying the target's descriptor mode."""
        self.prepare()
        assert self._signature is not None
        if self._receiver_name is None:
            return self._signature
        parameters = [
            parameter
            for parameter in self._signature.parameters.values()
            if parameter.name != self._receiver_name
        ]
        return self._signature.replace(parameters=parameters)

    def receiver(self, arguments: Mapping[str, object]) -> object | None:
        """Return the descriptor-bound receiver, independent of its parameter name."""
        self.prepare()
        if self._receiver_name is not None:
            return arguments.get(self._receiver_name)
        if self._variadic_receiver_name is not None:
            values = arguments.get(self._variadic_receiver_name, ())
            if isinstance(values, (tuple, list)) and values:
                return values[0]
        return None

    def application_arguments(self, arguments: Mapping[str, object]) -> dict[str, object]:
        """Project a bound call to its user parameters, excluding its real receiver."""
        self.prepare()
        projected = dict(arguments)
        if self._receiver_name is not None:
            projected.pop(self._receiver_name, None)
        if self._variadic_receiver_name is not None:
            values = projected.get(self._variadic_receiver_name, ())
            if isinstance(values, tuple) and values:
                projected[self._variadic_receiver_name] = values[1:]
        return projected

    def application_call(
        self, args: tuple[object, ...], kwargs: Mapping[str, object]
    ) -> tuple[tuple[object, ...], dict[str, object]]:
        """Build provider arguments after removing only a configured descriptor receiver."""
        self.prepare()
        assert self._signature is not None
        bound = self._signature.bind(*args, **dict(kwargs))
        bound.apply_defaults()
        projected = self.application_arguments(bound.arguments)
        application_bound = inspect.BoundArguments(self.application_signature, projected)
        return application_bound.args, application_bound.kwargs

    @property
    def requires_snapshot(self) -> bool:
        """Whether a new named selector caused this target to be inspected."""
        return self._signature is not None

    def validate(self, root: str, path: list[object]) -> None:
        self.prepare()
        if root == "input" and not self._user_parameter_names:
            raise ValueError(
                "Instrumentation input path requires a target with an application parameter"
            )
        if root == "params":
            if len(path) < 2 or type(path[1]) is not str:
                raise ValueError("Instrumentation params path must name a target parameter")
            parameter_name = cast(str, path[1])
            if parameter_name not in self._user_parameter_names:
                raise UnknownTargetParameterError(parameter_name)

    def resolve(self, invocation: dict[str, object], path: tuple[object, ...]) -> object:
        arguments = self.bind(invocation)
        return self.resolve_bound(arguments, path)

    def bind(self, invocation: dict[str, object]) -> dict[str, object]:
        self.prepare()
        assert self._signature is not None
        args = invocation.get("args", ())
        kwargs = invocation.get("kwargs", {})
        if type(args) not in (tuple, list) or type(kwargs) is not dict:
            raise ValueError("Instrumentation invocation arguments are unavailable")
        bound = self._signature.bind(*cast(tuple[object, ...] | list[object], args), **kwargs)
        bound.apply_defaults()
        arguments = bound.arguments
        if self._receiver_name is not None:
            arguments = {
                name: value for name, value in arguments.items() if name != self._receiver_name
            }
        if self._variadic_receiver_name is not None:
            arguments = dict(arguments)
            receiver_values = arguments.get(self._variadic_receiver_name, ())
            if type(receiver_values) is tuple and receiver_values:
                arguments[self._variadic_receiver_name] = receiver_values[1:]
        return arguments

    def snapshot(
        self,
        invocation: dict[str, object],
        snapshotter: Callable[[object], object],
    ) -> dict[str, object]:
        snapshot = snapshotter(self.bind(invocation))
        if type(snapshot) is not dict:
            raise ValueError("Instrumentation target arguments could not be snapshotted")
        return cast(dict[str, object], snapshot)

    def resolve_bound(self, arguments: Mapping[str, object], path: tuple[object, ...]) -> object:
        root = cast(str, path[0])
        if root == "input":
            parameter_name = self._user_parameter_names[0]
            current: object = arguments[parameter_name]
            return _walk_value_segments(current, path[1:], "input")
        parameter_name = cast(str, path[1])
        current = arguments[parameter_name]
        return _walk_value_segments(current, path[2:], f"params.{parameter_name}")


def target_arguments_for(target: Callable[..., object], descriptor_mode: str) -> _TargetArguments:
    """Create lazy signature metadata for an ordinary configured target."""
    return _TargetArguments(target, skip_receiver=descriptor_mode in _RECEIVER_MODES)


class _TargetValueReader:
    """A named source reader that can reuse an invocation-start argument snapshot."""

    def __init__(self, target_arguments: _TargetArguments, path: tuple[object, ...]) -> None:
        self._target_arguments = target_arguments
        self._path = path

    def __call__(self, invocation: dict[str, object]) -> object:
        return self._target_arguments.resolve(invocation, self._path)

    def resolve_bound(self, arguments: Mapping[str, object]) -> object:
        return self._target_arguments.resolve_bound(arguments, self._path)


def resolve_value_source(
    reader: Callable[[dict[str, object]], object],
    invocation: dict[str, object],
    parameter_snapshot: Mapping[str, object] | None = None,
) -> object:
    """Resolve a compiled source, reusing initial parameters only for named selectors."""
    if parameter_snapshot is not None and isinstance(reader, _TargetValueReader):
        return reader.resolve_bound(parameter_snapshot)
    return reader(invocation)


def resolve_binding(path: str, bindings: Mapping[str, object]) -> object:
    """Resolve an explicit dotted binding through mappings and ordinary data attrs."""
    if not isinstance(path, str) or not path:
        raise ValueError("Instrumentation binding path must be nonblank")
    parts = path.split(".")
    if any(not part.isidentifier() for part in parts):
        raise ValueError("Instrumentation binding path must contain identifier segments")
    if parts[0] not in bindings:
        raise ValueError(f"Missing binding for path root '{parts[0]}'")
    current: object = bindings[parts[0]]
    for part in parts[1:]:
        current = _safe_member(current, part, path)
    return current


def compile_value_source(
    source: Mapping[str, Any],
    bindings: Mapping[str, object],
    *,
    precheck: bool = True,
    target_arguments: _TargetArguments | None = None,
    native_input: bool = False,
) -> Callable[[dict[str, object]], object]:
    """Pre-resolve providers and selectors into a safe invocation-value reader."""
    if set(source) == {"value"}:
        value = copy.deepcopy(source["value"])
        return lambda _invocation: copy.deepcopy(value)
    if set(source) == {"path"}:
        path = source["path"]
        if not isinstance(path, list) or not path:
            raise ValueError("Instrumentation value path must be a nonempty list")
        root = path[0]
        if root not in _VALUE_ROOTS | {"input", "params"}:
            raise ValueError("Instrumentation value path has an unsupported root")
        if precheck and root in {"result", "error"}:
            raise ValueError("Precheck value paths cannot select result or error")
        if any(type(part) is not str and type(part) is not int for part in path[1:]):
            raise ValueError("Instrumentation value path segments must be strings or integers")
        if any(type(part) is int and part < 0 for part in path[1:]):
            raise ValueError("Instrumentation value path indices must be nonnegative")
        immutable_path = tuple(path)
        if root in {"input", "params"}:
            if target_arguments is not None:
                target_arguments.validate(root, path)
                return _TargetValueReader(target_arguments, immutable_path)
            if root == "params":
                raise ValueError(
                    "Instrumentation params selectors are unavailable for native tool arguments"
                )
            if not native_input:
                raise ValueError("Instrumentation input selector requires a configured target")
            return lambda invocation: _walk_value_segments(
                _native_input(invocation), immutable_path[1:], "input"
            )
        return lambda invocation: _walk_value_path(invocation, immutable_path)
    if set(source) == {"provider"}:
        provider = resolve_binding(source["provider"], bindings)
        if not callable(provider):
            raise ValueError("Instrumentation providers must be callable bindings")

        def call_provider(invocation: dict[str, object]) -> object:
            return provider(invocation)

        return call_provider
    raise ValueError("Invalid instrumentation value source")


def _walk_value_path(invocation: dict[str, object], path: tuple[object, ...]) -> object:
    root = path[0]
    current: object = invocation.get(str(root))
    return _walk_value_segments(current, path[1:], "value")


def _walk_value_segments(current: object, segments: tuple[object, ...], path: str) -> object:
    for segment in segments:
        if type(segment) is int:
            if type(current) not in (tuple, list):
                raise _MissingValuePath("Instrumentation value path is unavailable")
            sequence = cast(tuple[object, ...] | list[object], current)
            if segment >= len(sequence):
                raise _MissingValuePath("Instrumentation value path is unavailable")
            current = sequence[segment]
        else:
            current = _safe_member(current, str(segment), path)
    return current


def _native_input(invocation: dict[str, object]) -> object:
    args = invocation.get("args")
    if type(args) not in (tuple, list) or not args:
        raise _MissingValuePath("Instrumentation input path is unavailable")
    return cast(tuple[object, ...] | list[object], args)[0]


def _safe_member(owner: object, name: str, path: str) -> object:
    if type(owner) is dict:
        if name not in owner:
            raise _MissingValuePath(
                f"Instrumentation binding or value path '{path}' does not exist"
            )
        return owner[name]
    if isinstance(owner, types.ModuleType):
        namespace = vars(owner)
        if name not in namespace:
            raise _MissingValuePath(
                f"Instrumentation binding or value path '{path}' does not exist"
            )
        return namespace[name]
    if isinstance(owner, Mapping):
        raise ValueError("Instrumentation paths do not traverse custom Mapping objects")
    if type(owner).__getattribute__ is not object.__getattribute__:
        raise ValueError("Instrumentation paths do not traverse custom attribute accessors")
    instance_namespace: object
    try:
        instance_namespace = object.__getattribute__(owner, "__dict__")
    except (AttributeError, TypeError):
        instance_namespace = None
    if type(instance_namespace) is dict and name in instance_namespace:
        return instance_namespace[name]
    try:
        static_value = inspect.getattr_static(owner, name)
    except AttributeError:
        raise _MissingValuePath(
            f"Instrumentation binding or value path '{path}' does not exist"
        ) from None
    if isinstance(static_value, staticmethod):
        return static_value.__func__
    if isinstance(static_value, classmethod):
        cls = owner if isinstance(owner, type) else type(owner)
        return types.MethodType(static_value.__func__, cls)
    if inspect.isfunction(static_value):
        if isinstance(owner, type):
            return static_value
        return types.MethodType(static_value, owner)
    if inspect.isdatadescriptor(static_value) or inspect.ismethoddescriptor(static_value):
        raise ValueError("Instrumentation paths do not traverse descriptors")
    return static_value
