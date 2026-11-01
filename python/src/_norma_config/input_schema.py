"""Safe local Draft 7 input-schema compilation and validation."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import unquote

from jsonschema import Draft7Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry

_DRAFT7_URI = "http://json-schema.org/draft-07/schema#"
_SCHEMA_KEYWORDS = frozenset(Draft7Validator.META_SCHEMA["properties"])
_SUBSCHEMA_KEYS = {
    "additionalItems",
    "additionalProperties",
    "contains",
    "else",
    "if",
    "not",
    "propertyNames",
    "then",
}
_SUBSCHEMA_MAP_KEYS = {"definitions", "patternProperties", "properties"}
_SUBSCHEMA_ARRAY_KEYS = {"allOf", "anyOf", "oneOf"}


class InputSchemaError(ValueError):
    """A schema or instance failed the local input-schema contract."""

    def __init__(self, *, instance_path: str = "", definition: bool = False) -> None:
        self.instance_path = instance_path
        self.definition = definition
        super().__init__("Invalid input schema" if definition else "Input does not match schema")


@dataclass(frozen=True)
class CompiledInputSchema:
    validator: Draft7Validator

    def validate(self, value: object) -> str | None:
        try:
            error = next(self.validator.iter_errors(value), None)
        except Exception:
            # Treat malformed or cyclic runtime values as a safe validation failure.
            return ""
        if error is None:
            return None
        path = list(error.absolute_path)
        if error.validator == "required" and isinstance(error.instance, Mapping):
            missing = sorted(
                (name for name in error.validator_value if name not in error.instance),
                key=str,
            )
            if missing:
                path.append(missing[0])
        elif error.validator == "additionalProperties" and isinstance(error.instance, Mapping):
            properties = error.schema.get("properties", {})
            if isinstance(properties, Mapping):
                unexpected = sorted(set(error.instance) - set(properties), key=str)
                if unexpected:
                    path.append(unexpected[0])
        return _pointer(path)


def input_contract_path_status(
    schema: object,
    segments: list[str],
    *,
    require_non_null: bool = False,
) -> Literal["covered", "missing", "unknown"]:
    """Prove a dynamic input path from a deliberately narrow Draft 7 contract."""
    current = schema
    if not segments:
        current = _resolve_schema_reference(schema, current)
        if not isinstance(current, Mapping) or not current:
            return "unknown"
        if _has_unsupported_applicator(current):
            return "unknown"
        types = _schema_types(current)
        if types is None:
            return "unknown"
        if "null" not in types:
            return "covered"
        return (
            "missing"
            if require_non_null and set(types) == {"null"}
            else ("unknown" if require_non_null else "covered")
        )

    for index, segment in enumerate(segments):
        current = _resolve_schema_reference(schema, current)
        if not isinstance(current, Mapping) or _has_unsupported_applicator(current):
            return "unknown"
        if not _exclusive_object(current):
            return "unknown"
        properties = current.get("properties")
        required = current.get("required", [])
        if not isinstance(properties, Mapping):
            properties = {}
        if segment not in properties:
            if current.get("additionalProperties") is False and "patternProperties" not in current:
                return "missing"
            return "unknown"
        if not isinstance(required, list) or segment not in required:
            return "unknown"
        child = properties[segment]
        if index < len(segments) - 1:
            current = child
            continue
        current = _resolve_schema_reference(schema, child)
        if isinstance(current, bool):
            if current:
                return "unknown" if require_non_null else "covered"
            return "missing"
        if not isinstance(current, Mapping) or _has_unsupported_applicator(current):
            return "unknown"
        types = _schema_types(current)
        if types is None:
            return "unknown" if require_non_null else "covered"
        if "null" not in types:
            return "covered"
        if not require_non_null:
            return "covered"
        return "missing" if set(types) == {"null"} else "unknown"
    return "unknown"


def _resolve_schema_reference(root: object, schema: object) -> object:
    current = schema
    seen: set[str] = set()
    while isinstance(current, Mapping) and "$ref" in current:
        path, target = _resolve_local_pointer(root, str(current["$ref"]))
        if path in seen:
            return {}
        seen.add(path)
        current = target
    return current


def _schema_types(schema: Mapping[str, Any]) -> set[str] | None:
    value = schema.get("type")
    if isinstance(value, str):
        return {value}
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return set(value)
    return None


def _exclusive_object(schema: Mapping[str, Any]) -> bool:
    if _has_unsupported_applicator(schema):
        return False
    return _schema_types(schema) == {"object"}


def _has_unsupported_applicator(schema: Mapping[str, Any]) -> bool:
    return bool(
        set(schema)
        & {
            "allOf",
            "anyOf",
            "oneOf",
            "not",
            "if",
            "then",
            "else",
            "dependencies",
            "patternProperties",
        }
    )


def _pointer(parts: object) -> str:
    if not isinstance(parts, (list, tuple)):
        return ""
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def _resolve_local_pointer(root: object, reference: str) -> tuple[str, object]:
    if reference == "#":
        return "", root
    if not reference.startswith("#/"):
        raise ValueError("Only local JSON Pointer references are supported")
    fragment = unquote(reference[1:])
    if not fragment.startswith("/"):
        raise ValueError("Only local JSON Pointer references are supported")
    node = root
    tokens: list[str] = []
    for encoded in fragment[1:].split("/"):
        if re.search(r"~(?![01])", encoded):
            raise ValueError("Malformed JSON Pointer reference")
        token = encoded.replace("~1", "/").replace("~0", "~")
        tokens.append(token)
        if isinstance(node, Mapping) and token in node:
            node = node[token]
        elif isinstance(node, list) and token.isdecimal() and int(token) < len(node):
            node = node[int(token)]
        else:
            raise ValueError("Unresolved JSON Pointer reference")
    return "/" + "/".join(token.replace("~", "~0").replace("/", "~1") for token in tokens), node


def _schema_children(schema: Mapping[str, Any]) -> Iterator[tuple[tuple[str, ...], object]]:
    for key in _SUBSCHEMA_KEYS:
        value = schema.get(key)
        if isinstance(value, (Mapping, bool)):
            yield (key,), value
    for key in _SUBSCHEMA_MAP_KEYS:
        value = schema.get(key)
        if isinstance(value, Mapping):
            for name, child in value.items():
                if isinstance(child, (Mapping, bool)):
                    yield (key, str(name)), child
    value = schema.get("items")
    if isinstance(value, (Mapping, bool)):
        yield ("items",), value
    elif isinstance(value, list):
        for index, child in enumerate(value):
            if isinstance(child, (Mapping, bool)):
                yield ("items", str(index)), child
    for key in _SUBSCHEMA_ARRAY_KEYS:
        value = schema.get(key)
        if isinstance(value, list):
            for index, child in enumerate(value):
                if isinstance(child, (Mapping, bool)):
                    yield (key, str(index)), child
    dependencies = schema.get("dependencies")
    if isinstance(dependencies, Mapping):
        for name, child in dependencies.items():
            if isinstance(child, (Mapping, bool)):
                yield ("dependencies", str(name)), child


def _walk_schema(
    schema: object, path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], object]]:
    if isinstance(schema, bool):
        yield path, schema
        return
    if not isinstance(schema, Mapping):
        raise ValueError("Subschemas must be objects or booleans")
    yield path, schema
    for key, child in _schema_children(schema):
        yield from _walk_schema(child, (*path, *key))


def _check_schema_contract(schema: object) -> None:
    if not isinstance(schema, Mapping):
        raise ValueError("Schema must be an object")
    dialect = schema.get("$schema")
    if dialect is not None and dialect != _DRAFT7_URI:
        raise ValueError("Unsupported JSON Schema dialect")

    nodes = list(_walk_schema(schema))
    schema_paths = {_pointer(path) for path, _ in nodes}
    graph: dict[str, list[str]] = {}
    for path, node in nodes:
        source_path = _pointer(path)
        graph.setdefault(source_path, [])
        if not isinstance(node, Mapping):
            continue
        dialect = node.get("$schema")
        if dialect is not None and dialect != _DRAFT7_URI:
            raise ValueError("Unsupported JSON Schema dialect")
        for child_path, _ in _schema_children(node):
            graph[source_path].append(_pointer((*path, *child_path)))
        for key in node:
            if key not in _SCHEMA_KEYWORDS:
                raise ValueError("Unsupported JSON Schema keyword")
        if "$id" in node:
            raise ValueError(
                "Schema identifiers are unsupported; use local JSON Pointer references"
            )
        if "format" in node:
            raise ValueError("JSON Schema format annotations are unsupported")
        reference = node.get("$ref")
        if reference is not None:
            if not isinstance(reference, str):
                raise ValueError("Schema reference must be a string")
            target_path, target = _resolve_local_pointer(schema, reference)
            if target_path not in schema_paths:
                raise ValueError("JSON Pointer must target an actual schema position")
            if not isinstance(target, (Mapping, bool)):
                raise ValueError("JSON Pointer reference must target a schema")
            graph[source_path].append(target_path)
    try:
        Draft7Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError("Schema does not satisfy Draft 7") from exc

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(path: str) -> None:
        if path in visiting:
            raise ValueError("Cyclic JSON Pointer references are unsupported")
        if path in visited:
            return
        visiting.add(path)
        for target in graph.get(path, []):
            visit(target)
        visiting.remove(path)
        visited.add(path)

    for source in graph:
        visit(source)


def compile_input_schema(schema: object) -> CompiledInputSchema:
    """Validate and compile a local Draft 7 schema without external resolution."""
    try:
        _check_schema_contract(schema)
        validator = Draft7Validator(schema, registry=Registry())
    except (ValueError, SchemaError, TypeError, RecursionError):
        raise InputSchemaError(definition=True) from None
    return CompiledInputSchema(validator)


def validate_literal_input(schema: object, value: object) -> str | None:
    """Validate a literal value against a schema and return its instance pointer."""
    compiled = compile_input_schema(schema)
    return compiled.validate(value)
