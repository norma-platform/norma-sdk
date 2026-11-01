"""Strict JSON/YAML parsing and schema validation without SDK imports."""

from __future__ import annotations

import json
import math
from importlib import import_module
from importlib.resources import files
from typing import Any, Literal

import jsonschema

_SCHEMA = json.loads(files("_norma_config").joinpath("instrumentation.schema.json").read_text())
VALIDATOR = jsonschema.Draft202012Validator(_SCHEMA)


class LocatedParseError(ValueError):
    """Sanitized parser failure retaining its source position for diagnostics."""

    def __init__(self, message: str, line: int | None, column: int | None) -> None:
        super().__init__(message)
        self.line = line
        self.column = column


class InvalidInstrumentationRootError(ValueError):
    """A parsed JSON/YAML document is not the required object root."""


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-JSON numeric value: {value}")


def _safe_json_int(value: str) -> int:
    parsed = int(value)
    if value == "-0":
        raise ValueError("Negative zero is not supported in instrumentation config")
    if abs(parsed) > (2**53 - 1):
        raise ValueError("Integers outside the interoperable JSON range are not supported")
    return parsed


def _safe_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Nonfinite numbers are not supported in instrumentation config")
    if parsed == 0 and value.startswith("-"):
        raise ValueError("Negative zero is not supported in instrumentation config")
    return parsed


def parse_document(text: str, format: Literal["json", "yaml"] = "json") -> dict[str, Any]:
    """Parse one document using the shared strict JSON/YAML rules."""
    if format == "json":
        parsed = json.loads(
            text,
            object_pairs_hook=_json_object,
            parse_constant=_reject_constant,
            parse_int=_safe_json_int,
            parse_float=_safe_json_float,
        )
    elif format == "yaml":
        try:
            yaml_module = import_module("ruamel.yaml")
            duplicate_error = import_module("ruamel.yaml.constructor").DuplicateKeyError
        except ImportError:
            raise RuntimeError("YAML parsing requires the ruamel.yaml dependency") from None
        yaml = yaml_module.YAML(typ="safe")
        yaml.version = (1, 2)
        yaml.allow_duplicate_keys = False
        int_tag = "tag:yaml.org,2002:int"
        safe_int_constructor = yaml.constructor.yaml_constructors[int_tag]
        yaml.constructor.yaml_constructors = dict(yaml.constructor.yaml_constructors)

        def construct_yaml_int(constructor: Any, node: Any) -> int | float:
            value = safe_int_constructor(constructor, node)
            if value == 0 and node.value.strip().startswith("-"):
                return -0.0
            return value

        yaml.constructor.yaml_constructors[int_tag] = construct_yaml_int
        resolver = yaml.resolver.versioned_resolver
        for key, rules in resolver.items():
            resolver[key] = [
                (tag, pattern)
                for tag, pattern in rules
                if tag not in {"tag:yaml.org,2002:timestamp", "tag:yaml.org,2002:merge"}
            ]
        try:
            node = yaml.compose(text)
            if node is not None:
                nodes = import_module("ruamel.yaml.nodes")

                def reject_explicit_merge_tag(candidate: Any, visited: set[int]) -> None:
                    if id(candidate) in visited:
                        return
                    visited.add(id(candidate))
                    if candidate.tag == "tag:yaml.org,2002:merge":
                        raise ValueError(
                            "Explicit YAML merge tags are not supported in instrumentation config"
                        )
                    if isinstance(candidate, nodes.MappingNode):
                        for key_node, value_node in candidate.value:
                            reject_explicit_merge_tag(key_node, visited)
                            reject_explicit_merge_tag(value_node, visited)
                    elif isinstance(candidate, nodes.SequenceNode):
                        for item_node in candidate.value:
                            reject_explicit_merge_tag(item_node, visited)

                reject_explicit_merge_tag(node, set())
            parsed = yaml.load(text)
        except duplicate_error as exc:
            raise ValueError("Duplicate YAML key in instrumentation config") from exc
        except Exception as exc:
            yaml_error = import_module("ruamel.yaml.error").YAMLError
            if isinstance(exc, yaml_error):
                mark = getattr(exc, "problem_mark", None)
                line = mark.line + 1 if mark is not None else None
                column = mark.column + 1 if mark is not None else None
                raise LocatedParseError(
                    "Invalid YAML instrumentation config", line, column
                ) from None
            raise
        if any(
            info.doc_version is not None
            and (info.doc_version.major, info.doc_version.minor) != (1, 2)
            for info in yaml.doc_infos
        ):
            raise ValueError("YAML instrumentation must use YAML 1.2")
    else:
        raise ValueError("format must be 'json' or 'yaml'")
    if type(parsed) is not dict:
        raise InvalidInstrumentationRootError("Invalid instrumentation at $: expected an object")
    return parsed


def json_safe_config(config: dict[str, Any]) -> dict[str, Any]:
    """Copy a parsed document through strict JSON semantics before traversal."""
    try:
        return json.loads(
            json.dumps(config, allow_nan=False),
            parse_int=_safe_json_int,
            parse_float=_safe_json_float,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"Instrumentation config must contain only JSON values: {exc}") from None


def normalize_turn_aliases(config: dict[str, Any]) -> None:
    """Apply the shared compatibility aliases and reject conflicting values."""
    for entrypoint in config["entrypoints"]:
        if not isinstance(entrypoint, dict):
            continue
        turn = entrypoint.get("turn")
        if not isinstance(turn, dict):
            continue
        for alias, legacy in (
            ("application", "platform"),
            ("application_version", "platform_version"),
        ):
            if alias not in turn:
                continue
            if legacy in turn and turn[legacy] != turn[alias]:
                raise ValueError(f"turn.{alias} conflicts with turn.{legacy}")
            turn[legacy] = turn.pop(alias)
