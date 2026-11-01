"""Offline structural checks for local Rule selector coverage."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict

from .input_schema import input_contract_path_status
from .parser import (
    LocatedParseError,
    _json_object,
    _reject_constant,
    _safe_json_float,
    _safe_json_int,
)
from .validation import _Diagnostic, validate_instrumentation_source

_UNKNOWN_OBJECT = object()


@dataclass(frozen=True)
class _PartialObject:
    known: Mapping[str, object]
    known_missing: frozenset[str] = frozenset()


class _CoverageItem(TypedDict):
    file: str
    rule_file: str
    rule_index: int
    rule_path: str
    checkpoint: str
    stage: Literal["pre", "post"]
    selector: str
    status: Literal["covered", "missing", "unknown"]
    diagnostic_path: str


def _pointer(parts: tuple[str | int, ...] | list[str | int]) -> str:
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def _base_parts(pointer: str) -> tuple[str, ...]:
    return tuple(pointer[1:].split("/")) if pointer else ()


def _read_supported_files(source: str | Path, *, kind: str) -> tuple[list[Path], list[_Diagnostic]]:
    path = Path(source).expanduser().absolute()
    diagnostics: list[_Diagnostic] = []
    if path.is_dir():
        try:
            files = sorted(
                (
                    item
                    for item in path.iterdir()
                    if item.is_file() and item.suffix.lower() in {".json", ".yaml", ".yml"}
                ),
                key=lambda item: item.name,
            )
        except OSError:
            return [], [_Diagnostic(str(path), "io", "", f"{kind} directory could not be read.")]
        if not files:
            diagnostics.append(
                _Diagnostic(str(path), "io", "", f"No supported {kind} files were found.")
            )
        return files, diagnostics
    if path.is_file() and path.suffix.lower() in {".json", ".yaml", ".yml"}:
        return [path], []
    return [], [_Diagnostic(str(path), "io", "", f"Expected a JSON/YAML {kind} file or directory.")]


def _parse_any_document(text: str, fmt: Literal["json", "yaml"]) -> object:
    if fmt == "json":
        value = json.loads(
            text,
            object_pairs_hook=_json_object,
            parse_constant=_reject_constant,
            parse_int=_safe_json_int,
            parse_float=_safe_json_float,
        )
        json.dumps(value, allow_nan=False)
        return value
    from importlib import import_module

    yaml_module = import_module("ruamel.yaml")
    yaml = yaml_module.YAML(typ="safe")
    yaml.version = (1, 2)
    yaml.allow_duplicate_keys = False
    yaml_error = import_module("ruamel.yaml.error").YAMLError
    try:
        value = yaml.load(text)
    except Exception as exc:
        if isinstance(exc, yaml_error):
            mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
            line = mark.line + 1 if mark is not None else None
            column = mark.column + 1 if mark is not None else None
            raise LocatedParseError("Invalid YAML Rule file", line, column) from None
        raise
    json.dumps(value, allow_nan=False)
    return value


def _parse_rule_file(file: Path) -> tuple[object | None, _Diagnostic | None]:
    try:
        text = file.read_text(encoding="utf-8")
        fmt: Literal["json", "yaml"] = "json" if file.suffix.lower() == ".json" else "yaml"
        return _parse_any_document(text, fmt), None
    except (OSError, UnicodeError):
        return None, _Diagnostic(str(file), "io", "", "Rule file could not be read as UTF-8.")
    except json.JSONDecodeError as exc:
        return None, _Diagnostic(
            str(file), "syntax", "", "Invalid JSON/YAML Rule syntax.", exc.lineno, exc.colno
        )
    except LocatedParseError as exc:
        return None, _Diagnostic(
            str(file), "syntax", "", "Invalid JSON/YAML Rule syntax.", exc.line, exc.column
        )
    except (ValueError, TypeError, RecursionError):
        return None, _Diagnostic(str(file), "syntax", "", "Invalid JSON/YAML Rule syntax.")


def _unpack_rules(
    value: object,
) -> tuple[list[tuple[dict[str, Any], str, str | None]], str | None, str | None]:
    """Return flattened Rule definitions, draft identity, and safe shape error path."""
    draft_agent: str | None = None
    if isinstance(value, Mapping) and "jsonText" in value:
        if not isinstance(value.get("jsonText"), str):
            return [], None, "/jsonText"
        if "agentName" in value and not isinstance(value.get("agentName"), str):
            return [], None, "/agentName"
        draft_agent = value.get("agentName")
        try:
            value = _parse_any_document(value["jsonText"], "json")
        except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
            return [], draft_agent, "/jsonText"
    if isinstance(value, Mapping) and "previousDraft" in value:
        value = {key: item for key, item in value.items() if key != "previousDraft"}

    entries: list[tuple[object, tuple[str | int, ...]]]
    if isinstance(value, Mapping):
        if isinstance(value.get("rules"), list):
            entries = [(item, ("rules", index)) for index, item in enumerate(value["rules"])]
        elif isinstance(value.get("rule"), list):
            entries = [(item, ("rule", index)) for index, item in enumerate(value["rule"])]
        elif isinstance(value.get("rule"), Mapping):
            entries = [(value, ("rule",))]
        elif "condition" in value or "selector" in value:
            entries = [(value, ())]
        else:
            return [], draft_agent, ""
    elif isinstance(value, list):
        entries = [(item, (index,)) for index, item in enumerate(value)]
    else:
        return [], draft_agent, ""

    result: list[tuple[dict[str, Any], str, str | None]] = []
    for raw, path_parts in entries:
        base = _pointer(path_parts)
        if not isinstance(raw, Mapping):
            return [], draft_agent, base
        wrapper_scope: object = raw.get("scope")
        definition = raw.get("definition", raw.get("rule", raw))
        if not isinstance(definition, Mapping):
            return [], draft_agent, base
        copied_definition = dict(definition)
        if wrapper_scope is not None and "scope" not in copied_definition:
            copied_definition["scope"] = wrapper_scope
        result.append((copied_definition, base, draft_agent))
    return result, draft_agent, None


def _condition_selectors(
    node: object,
    path: tuple[str | int, ...],
    declarations: list[tuple[str, str, bool, bool, bool]],
    diagnostics: list[_Diagnostic],
    file: Path,
    rule_file: Path,
) -> None:
    if not isinstance(node, Mapping):
        diagnostics.append(
            _Diagnostic(
                str(rule_file), "rules_schema", _pointer(path), "Rule condition is invalid."
            )
        )
        return
    branches = [key for key in ("and", "or", "not") if key in node]
    has_selector = "selector" in node
    has_evaluator = "evaluator" in node
    if branches:
        if len(branches) != 1 or set(node) != {branches[0]}:
            diagnostics.append(
                _Diagnostic(
                    str(rule_file), "rules_schema", _pointer(path), "Rule condition is invalid."
                )
            )
            return
        key = branches[0]
        children = node[key]
        if key == "not":
            _condition_selectors(children, (*path, key), declarations, diagnostics, file, rule_file)
        elif not isinstance(children, list) or not children:
            diagnostics.append(
                _Diagnostic(
                    str(rule_file),
                    "rules_schema",
                    _pointer((*path, key)),
                    "Rule condition is invalid.",
                )
            )
        else:
            for index, child in enumerate(children):
                _condition_selectors(
                    child, (*path, key, index), declarations, diagnostics, file, rule_file
                )
        return
    if set(node) - {"selector", "evaluator"}:
        diagnostics.append(
            _Diagnostic(
                str(rule_file), "rules_schema", _pointer(path), "Rule condition is invalid."
            )
        )
        return
    if not has_evaluator or not isinstance(node.get("evaluator"), Mapping):
        diagnostics.append(
            _Diagnostic(
                str(rule_file), "rules_schema", _pointer(path), "Rule condition is invalid."
            )
        )
        return
    evaluator = node["evaluator"]
    if not isinstance(evaluator.get("name"), str) or not isinstance(
        evaluator.get("config"), Mapping
    ):
        diagnostics.append(
            _Diagnostic(
                str(rule_file),
                "rules_schema",
                _pointer((*path, "evaluator")),
                "Rule evaluator is invalid.",
            )
        )
        return
    semantic = evaluator["name"] in {"semantic", "finish_claims"}
    selector = "*"
    if has_selector:
        raw_selector = node["selector"]
        if not isinstance(raw_selector, Mapping) or not isinstance(raw_selector.get("path"), str):
            diagnostics.append(
                _Diagnostic(
                    str(rule_file),
                    "rules_schema",
                    _pointer((*path, "selector", "path")),
                    "Rule selector is invalid.",
                )
            )
            return
        selector = raw_selector["path"]
        if selector != "*" or not semantic:
            declarations.append(
                (_pointer((*path, "selector", "path")), selector, False, False, False)
            )
    elif not semantic:
        diagnostics.append(
            _Diagnostic(
                str(rule_file),
                "rules_schema",
                _pointer((*path, "selector")),
                "Rule selector is required.",
            )
        )
        return

    config = evaluator["config"]
    if evaluator["name"] == "json" and isinstance(config, Mapping):
        if "required_fields" in config:
            required = config["required_fields"]
            allow_null_required = config.get("allow_null_required", False)
            if not isinstance(allow_null_required, bool):
                diagnostics.append(
                    _Diagnostic(
                        str(rule_file),
                        "rules_schema",
                        _pointer((*path, "evaluator", "config", "allow_null_required")),
                        "Rule null handling option is invalid.",
                    )
                )
            if not isinstance(required, list):
                diagnostics.append(
                    _Diagnostic(
                        str(rule_file),
                        "rules_schema",
                        _pointer((*path, "evaluator", "config", "required_fields")),
                        "Rule dependencies are invalid.",
                    )
                )
            else:
                for index, field in enumerate(required):
                    decl_path = _pointer((*path, "evaluator", "config", "required_fields", index))
                    if not isinstance(field, str):
                        diagnostics.append(
                            _Diagnostic(
                                str(rule_file),
                                "rules_schema",
                                decl_path,
                                "Rule dependency is invalid.",
                            )
                        )
                    else:
                        declarations.append(
                            (
                                decl_path,
                                _join_selector(selector, field),
                                not allow_null_required,
                                False,
                                False,
                            )
                        )

        for field_map_name in ("field_types", "field_constraints"):
            if field_map_name not in config:
                continue
            field_map = config[field_map_name]
            map_path = (*path, "evaluator", "config", field_map_name)
            if field_map is None:
                continue
            if not isinstance(field_map, Mapping):
                diagnostics.append(
                    _Diagnostic(
                        str(rule_file),
                        "rules_schema",
                        _pointer(map_path),
                        "Rule field dependencies are invalid.",
                    )
                )
                continue
            for field_path in field_map:
                if not isinstance(field_path, str) or not field_path:
                    diagnostics.append(
                        _Diagnostic(
                            str(rule_file),
                            "rules_schema",
                            _pointer((*map_path, "<key>")),
                            "Rule field dependency key is invalid.",
                        )
                    )
                    continue
                field_value = field_map[field_path]
                if field_map_name == "field_types" and not isinstance(field_value, str):
                    diagnostics.append(
                        _Diagnostic(
                            str(rule_file),
                            "rules_schema",
                            _pointer((*map_path, field_path)),
                            "Rule field type dependency is invalid.",
                        )
                    )
                    continue
                if field_map_name == "field_constraints" and not isinstance(field_value, Mapping):
                    diagnostics.append(
                        _Diagnostic(
                            str(rule_file),
                            "rules_schema",
                            _pointer((*map_path, field_path)),
                            "Rule field constraint dependency is invalid.",
                        )
                    )
                    continue
                declarations.append(
                    (
                        _pointer((*map_path, field_path)),
                        _join_selector(selector, field_path),
                        False,
                        False,
                        False,
                    )
                )
    if evaluator["name"] == "semantic" and isinstance(config, Mapping) and "bindings" in config:
        bindings = config["bindings"]
        if not isinstance(bindings, list):
            diagnostics.append(
                _Diagnostic(
                    str(rule_file),
                    "rules_schema",
                    _pointer((*path, "evaluator", "config", "bindings")),
                    "Rule bindings are invalid.",
                )
            )
        else:
            for index, binding in enumerate(bindings):
                base = (*path, "evaluator", "config", "bindings", index)
                if not isinstance(binding, Mapping):
                    diagnostics.append(
                        _Diagnostic(
                            str(rule_file),
                            "rules_schema",
                            _pointer(base),
                            "Rule binding is invalid.",
                        )
                    )
                    continue
                required_binding = binding.get("required", True)
                if not isinstance(required_binding, bool):
                    diagnostics.append(
                        _Diagnostic(
                            str(rule_file),
                            "rules_schema",
                            _pointer((*base, "required")),
                            "Rule binding required flag is invalid.",
                        )
                    )
                    continue
                binding_path = binding.get("path")
                if not isinstance(binding_path, str) or not binding_path:
                    diagnostics.append(
                        _Diagnostic(
                            str(rule_file),
                            "rules_schema",
                            _pointer((*base, "path")),
                            "Rule binding path is required.",
                        )
                    )
                    continue
                if not required_binding:
                    continue
                declarations.append((_pointer((*base, "path")), binding_path, True, False, True))
                if "max_age_seconds" in binding:
                    observed_path = binding.get("observed_at_path")
                    if not isinstance(observed_path, str):
                        diagnostics.append(
                            _Diagnostic(
                                str(rule_file),
                                "rules_schema",
                                _pointer((*base, "observed_at_path")),
                                "Rule observed_at_path is required.",
                            )
                        )
                    else:
                        declarations.append(
                            (
                                _pointer((*base, "observed_at_path")),
                                observed_path,
                                True,
                                False,
                                True,
                            )
                        )

    evaluator_name = evaluator["name"]
    if evaluator_name == "semantic" and not (
        isinstance(config, Mapping)
        and isinstance(config.get("bindings"), list)
        and config["bindings"]
    ):
        declarations.append((_pointer((*path, "evaluator")), "*", True, True, False))
    elif evaluator_name in {"rego", "sql", "custom", "finish_claims"} or evaluator_name not in {
        "json",
        "regex",
        "list",
        "semantic",
    }:
        declarations.append((_pointer((*path, "evaluator")), "*", True, True, False))
    elif evaluator_name == "json" and set(config) - {
        "required_fields",
        "allow_null_required",
        "field_types",
        "field_constraints",
    }:
        declarations.append((_pointer((*path, "evaluator", "config")), "*", True, True, False))


def _join_selector(base: str, suffix: str) -> str:
    return suffix if base == "*" else f"{base}.{suffix}"


def _selector_parts(selector: str) -> tuple[str, list[str]] | None:
    if (
        not selector
        or any(char in selector for char in "*?[]{}$")
        or any(char.isspace() for char in selector)
    ):
        return None
    parts = selector.split(".")
    if any(not part for part in parts):
        return None
    return parts[0], parts[1:]


def _at_path(
    value: object, parts: list[str], *, numeric_indices: bool = False
) -> Literal["covered", "missing", "unknown"]:
    current = value
    for part in parts:
        if current is _UNKNOWN_OBJECT:
            return "unknown"
        if isinstance(current, _PartialObject):
            if part in current.known:
                current = current.known[part]
                continue
            if part in current.known_missing:
                return "missing"
            return "unknown"
        if isinstance(current, list):
            if not numeric_indices or not part.isdecimal():
                return "missing"
            index = int(part)
            if index >= len(current):
                return "missing"
            current = current[index]
            continue
        if not isinstance(current, Mapping):
            return "unknown" if current is not None else "missing"
        if part not in current:
            return "missing"
        current = current[part]
    return "covered"


def _checkpoint_root(
    config: Mapping[str, Any], checkpoint: Mapping[str, Any], root: str, stage: str
) -> tuple[bool, object]:
    if root == "input":
        source = checkpoint.get("input_from")
        if source is None:
            return True, _UNKNOWN_OBJECT
        if isinstance(source, str) and source == "all_kwargs":
            return True, _UNKNOWN_OBJECT
        if isinstance(source, Mapping) and set(source) == {"value"}:
            return True, source["value"]
        return True, _UNKNOWN_OBJECT
    if root == "output":
        if stage == "pre":
            return True, None
        source = checkpoint.get("output_from")
        if isinstance(source, Mapping) and set(source) == {"value"}:
            return True, source["value"]
        return True, _UNKNOWN_OBJECT
    if root == "context":
        if isinstance(checkpoint.get("state"), Mapping):
            return True, _PartialObject({"state": checkpoint["state"]})
        state_source = checkpoint.get("state_from")
        if isinstance(state_source, Mapping) and set(state_source) == {"value"}:
            return True, _PartialObject({"state": state_source["value"]})
        if state_source is not None:
            return True, _PartialObject({})
        options = checkpoint.get("options")
        if isinstance(options, Mapping) and isinstance(options.get("state"), Mapping):
            state = options["state"]
            if "binding" in state:
                return True, _PartialObject({})
            return True, _PartialObject({"state": state})
        if checkpoint.get("state_provider") is not None or isinstance(options, str):
            return True, _PartialObject({})
        if checkpoint.get("context_from") is not None:
            return True, _PartialObject({})
        if any(
            isinstance(entry, Mapping) and entry.get("context") is not None
            for entry in config.get("entrypoints", [])
        ):
            return True, _PartialObject({})
        return True, _PartialObject({}, frozenset({"state"}))
    return False, None


def _selector_status(
    config: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    selector: str,
    stage: str,
    *,
    required_nonnull: bool = False,
    numeric_indices: bool = False,
) -> Literal["covered", "missing", "unknown"]:
    parsed = _selector_parts(selector)
    if parsed is None:
        return "unknown"
    root, parts = parsed
    if root == "name":
        return "covered" if not parts else "missing"
    if root == "type":
        return "covered" if not parts else "missing"
    if root not in {"input", "output", "context"}:
        return "unknown"
    known, value = _checkpoint_root(config, checkpoint, root, stage)
    if not known:
        return "unknown"
    input_source = checkpoint.get("input_from")
    literal_input = isinstance(input_source, Mapping) and set(input_source) == {"value"}
    if root == "input" and not literal_input and "input_schema" in checkpoint:
        status = input_contract_path_status(
            checkpoint["input_schema"], parts, require_non_null=required_nonnull
        )
        return status
    status = _at_path(value, parts, numeric_indices=numeric_indices)
    if (
        required_nonnull
        and status == "covered"
        and _path_value(value, parts, numeric_indices=numeric_indices) is None
    ):
        return "missing"
    return status


def _path_value(value: object, parts: list[str], *, numeric_indices: bool = False) -> object:
    current = value
    for part in parts:
        if isinstance(current, list) and numeric_indices and part.isdecimal():
            index = int(part)
            if index >= len(current):
                return _UNAVAILABLE
            current = current[index]
            continue
        if not isinstance(current, Mapping) or part not in current:
            return _UNAVAILABLE
        current = current[part]
    return current


_UNAVAILABLE = object()


def _scope_status(
    scope: Mapping[str, Any] | None,
    name: str,
    checkpoint: Mapping[str, Any],
    stage: str,
) -> str:
    if scope is None:
        return "apply"
    names = scope.get("step_names")
    exact = isinstance(names, list) and name in names
    step_type = checkpoint.get("step_type", "tool")
    if isinstance(scope.get("step_types"), list) and step_type not in scope["step_types"]:
        return "skip"
    if isinstance(scope.get("stages"), list) and stage not in scope["stages"]:
        return "skip"
    if isinstance(names, list) and not exact:
        if isinstance(scope.get("step_name_regex"), str):
            return "unknown"
        return "skip"
    if isinstance(scope.get("step_name_regex"), str) and not exact:
        return "unknown"
    return "apply"


def _scope_error(scope: object) -> bool:
    allowed = {"step_names", "step_types", "stages", "step_name_regex"}
    return (
        not isinstance(scope, Mapping)
        or bool(set(scope) - allowed)
        or (
            "step_names" in scope
            and scope["step_names"] is not None
            and (
                not isinstance(scope["step_names"], list)
                or not scope["step_names"]
                or not all(isinstance(v, str) for v in scope["step_names"])
            )
        )
        or (
            "step_types" in scope
            and scope["step_types"] is not None
            and (
                not isinstance(scope["step_types"], list)
                or not scope["step_types"]
                or not all(isinstance(v, str) for v in scope["step_types"])
            )
        )
        or (
            "stages" in scope
            and scope["stages"] is not None
            and (
                not isinstance(scope["stages"], list)
                or not scope["stages"]
                or not all(v in {"pre", "post"} for v in scope["stages"])
            )
        )
        or (
            "step_name_regex" in scope
            and scope["step_name_regex"] is not None
            and not isinstance(scope["step_name_regex"], str)
        )
    )


def _checkpoint_stages(checkpoint: Mapping[str, Any]) -> list[str]:
    return ["pre", "post"]


def _rule_file_for_config(config: Path, rules_path: Path, rule_files: list[Path]) -> Path | None:
    if rules_path.is_file():
        return rule_files[0] if rule_files else None
    matches = [item for item in rule_files if item.stem == config.stem]
    return matches[0] if len(matches) == 1 else None


def _coverage_for_config(
    config_file: Path,
    rule_file: Path,
    config: Mapping[str, Any],
    rules: list[tuple[dict[str, Any], str, str | None]],
) -> tuple[list[_CoverageItem], list[_Diagnostic], int]:
    coverage: list[_CoverageItem] = []
    diagnostics: list[_Diagnostic] = []
    rules_checked = 0
    checkpoints = config.get("checkpoints", {})
    if not isinstance(checkpoints, Mapping):
        return coverage, diagnostics, rules_checked
    for index, (definition, base_path, draft_agent) in enumerate(rules):
        rules_checked += 1
        if draft_agent and draft_agent != config_file.stem:
            diagnostics.append(
                _Diagnostic(
                    str(rule_file),
                    "rules_scope",
                    "/agentName",
                    "Rule draft agent does not match the configuration.",
                )
            )
            continue
        if "enabled" in definition and not isinstance(definition["enabled"], bool):
            diagnostics.append(
                _Diagnostic(
                    str(rule_file),
                    "rules_schema",
                    _pointer((*_base_parts(base_path), "enabled")),
                    "Rule enabled must be a boolean.",
                )
            )
            continue
        scope = definition.get("scope")
        declarations: list[tuple[str, str, bool, bool, bool]] = []
        condition = definition.get("condition")
        base_parts = _base_parts(base_path)
        diagnostics_before_condition = len(diagnostics)
        try:
            if condition is not None:
                _condition_selectors(
                    condition,
                    (*base_parts, "condition"),
                    declarations,
                    diagnostics,
                    config_file,
                    rule_file,
                )
            elif isinstance(definition.get("selector"), Mapping) and isinstance(
                definition.get("evaluator"), Mapping
            ):
                _condition_selectors(
                    {"selector": definition["selector"], "evaluator": definition["evaluator"]},
                    base_parts,
                    declarations,
                    diagnostics,
                    config_file,
                    rule_file,
                )
            else:
                diagnostics.append(
                    _Diagnostic(
                        str(rule_file),
                        "rules_schema",
                        _pointer((*base_parts, "condition")),
                        "Rule condition is required.",
                    )
                )
        except RecursionError:
            diagnostics.append(
                _Diagnostic(
                    str(rule_file),
                    "rules_schema",
                    _pointer((*base_parts, "condition")),
                    "Rule condition is too deeply nested to inspect.",
                )
            )
            continue
        if len(diagnostics) != diagnostics_before_condition:
            continue
        if scope is not None and _scope_error(scope):
            diagnostics.append(
                _Diagnostic(
                    str(rule_file),
                    "rules_scope",
                    _pointer((*base_parts, "scope")),
                    "Rule scope is invalid.",
                )
            )
            continue
        had_applicable = False
        if definition.get("enabled", True) is False:
            continue
        for checkpoint_name, raw_checkpoint in checkpoints.items():
            if not isinstance(raw_checkpoint, Mapping):
                continue
            for stage in _checkpoint_stages(raw_checkpoint):
                applicability = _scope_status(scope, str(checkpoint_name), raw_checkpoint, stage)
                if applicability == "skip":
                    continue
                if applicability == "unknown":
                    coverage.append(
                        {
                            "file": str(config_file.absolute()),
                            "rule_file": str(rule_file.absolute()),
                            "rule_index": index,
                            "rule_path": _pointer((*base_parts, "scope")),
                            "checkpoint": str(checkpoint_name),
                            "stage": stage,  # type: ignore[typeddict-item]
                            "selector": "*",
                            "status": "unknown",
                            "diagnostic_path": _pointer((*base_parts, "scope")),
                        }
                    )
                    had_applicable = True
                    continue
                had_applicable = True
                for (
                    path,
                    selector,
                    required_nonnull,
                    forced_unknown,
                    numeric_indices,
                ) in declarations:
                    status = (
                        "unknown"
                        if forced_unknown
                        else _selector_status(
                            config,
                            raw_checkpoint,
                            selector,
                            stage,
                            required_nonnull=required_nonnull,
                            numeric_indices=numeric_indices,
                        )
                    )
                    coverage.append(
                        {
                            "file": str(config_file.absolute()),
                            "rule_file": str(rule_file.absolute()),
                            "rule_index": index,
                            "rule_path": path,
                            "checkpoint": str(checkpoint_name),
                            "stage": stage,  # type: ignore[typeddict-item]
                            "selector": selector,
                            "status": status,
                            "diagnostic_path": path,
                        }
                    )
        if not had_applicable:
            scope_path = _pointer((*base_parts, "scope"))
            coverage.append(
                {
                    "file": str(config_file.absolute()),
                    "rule_file": str(rule_file.absolute()),
                    "rule_index": index,
                    "rule_path": scope_path,
                    "checkpoint": "",
                    "stage": "pre",
                    "selector": "*",
                    "status": "unknown",
                    "diagnostic_path": scope_path,
                }
            )
    return coverage, diagnostics, rules_checked


def check_rule_coverage(
    config_source: str | Path, rules_source: str | Path, *, allow_unknown: bool = False
) -> tuple[dict[str, Any], bool]:
    """Validate config then compare selector dependencies to static local shapes."""
    report: dict[str, Any] = dict(validate_instrumentation_source(config_source))
    report.update(rules_checked=0, coverage_complete=False, coverage=[])
    unsupported_input_files = {
        item["file"]
        for item in report["diagnostics"]
        if item["code"] == "capability"
        and item["path"].startswith("/checkpoints/")
        and item["path"].endswith("/input_from")
    }
    invalid_input_contract_files = {
        item["file"]
        for item in report["diagnostics"]
        if item["code"] == "schema"
        and item["path"].startswith("/checkpoints/")
        and (item["path"].endswith("/input_schema") or item["path"].endswith("/input_from/value"))
    }
    coverage: list[_CoverageItem] = []
    diagnostics: list[_Diagnostic] = []
    config_files, config_io = _read_supported_files(config_source, kind="configuration")
    rule_files, rules_io = _read_supported_files(rules_source, kind="Rule")
    diagnostics.extend(config_io)
    diagnostics.extend(rules_io)
    rules_path = Path(rules_source).expanduser().absolute()
    if rules_path.is_dir():
        stems: set[str] = set()
        for rule_file in rule_files:
            if rule_file.stem in stems:
                diagnostics.append(
                    _Diagnostic(
                        str(rule_file),
                        "identity",
                        "",
                        "Multiple Rule files use the same configuration name.",
                    )
                )
            stems.add(rule_file.stem)
    checked = 0
    for config_file in config_files:
        selected_rule_file = _rule_file_for_config(config_file, rules_path, rule_files)
        if selected_rule_file is None:
            diagnostics.append(
                _Diagnostic(str(config_file), "io", "", "No matching Rule file was found.")
            )
            continue
        parsed_config, config_error = _parse_rule_file(config_file)
        parsed_rules, rules_error = _parse_rule_file(selected_rule_file)
        if config_error is not None:
            continue  # The configuration validator already reported this file.
        if rules_error is not None:
            diagnostics.append(
                _Diagnostic(
                    rules_error.file,
                    rules_error.code,
                    rules_error.path,
                    "Rule file is invalid.",
                    rules_error.line,
                    rules_error.column,
                )
            )
            continue
        if not isinstance(parsed_config, Mapping) or not isinstance(parsed_rules, (Mapping, list)):
            diagnostics.append(
                _Diagnostic(
                    str(selected_rule_file),
                    "rules_schema",
                    "",
                    "Rule document has an unsupported shape.",
                )
            )
            continue
        parsed_rule_list, draft_agent, shape_error = _unpack_rules(parsed_rules)
        if shape_error is not None:
            diagnostics.append(
                _Diagnostic(
                    str(selected_rule_file),
                    "rules_schema",
                    shape_error,
                    "Rule document has an unsupported shape.",
                )
            )
            continue
        if draft_agent and draft_agent != config_file.stem:
            diagnostics.append(
                _Diagnostic(
                    str(selected_rule_file),
                    "rules_scope",
                    "/agentName",
                    "Rule draft agent does not match the configuration.",
                )
            )
            continue
        if str(config_file.absolute()) in unsupported_input_files | invalid_input_contract_files:
            # The Python SDK cannot establish any runtime shape for this
            # TypeScript-only source, so do not emit derived path coverage.
            continue
        current, rule_diagnostics, rule_count = _coverage_for_config(
            config_file, selected_rule_file, parsed_config, parsed_rule_list
        )
        coverage.extend(current)
        diagnostics.extend(rule_diagnostics)
        checked += rule_count
    diagnostics.extend(
        _Diagnostic(
            item["file"],
            "coverage_missing" if item["status"] == "missing" else "coverage_unknown",
            item["diagnostic_path"],
            "A Rule selector path is absent from this configured checkpoint."
            if item["status"] == "missing"
            else "A Rule selector path cannot be proven from static configuration.",
        )
        for item in coverage
        if item["status"] == "missing" or (item["status"] == "unknown" and not allow_unknown)
    )
    coverage.sort(
        key=lambda item: (
            item["file"],
            item["rule_index"],
            item["checkpoint"],
            0 if item["stage"] == "pre" else 1,
            item["diagnostic_path"],
        )
    )
    all_diagnostics: list[dict[str, Any]] = [
        {
            "file": item.file,
            "code": item.code,
            "path": item.path,
            "message": item.message,
            "line": item.line,
            "column": item.column,
        }
        for item in diagnostics
    ]
    all_diagnostics.extend(report["diagnostics"])
    all_diagnostics.sort(key=lambda item: (item["file"], item["path"], item["code"]))
    report.update(
        rules_checked=checked,
        coverage_complete=(
            not report["diagnostics"]
            and not diagnostics
            and all(item["status"] == "covered" for item in coverage)
        ),
        coverage=[
            {key: value for key, value in item.items() if key != "diagnostic_path"}
            for item in coverage
        ],
        diagnostics=all_diagnostics,
        valid=(not all_diagnostics),
    )
    unknown_present = any(item["status"] == "unknown" for item in coverage)
    if allow_unknown and unknown_present and report["valid"]:
        return report, True
    return report, False
