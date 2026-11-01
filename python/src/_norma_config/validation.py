"""Offline validation for Python declarative instrumentation files."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict

from jsonschema.exceptions import ValidationError

from .identity import ensure_agent_name
from .input_schema import InputSchemaError, compile_input_schema
from .observed_state import copy_observed_state
from .parser import (
    VALIDATOR,
    InvalidInstrumentationRootError,
    LocatedParseError,
    json_safe_config,
    normalize_turn_aliases,
    parse_document,
)


class _DiagnosticRecord(TypedDict):
    file: str
    code: str
    path: str
    message: str
    line: int | None
    column: int | None


class _ValidationReport(TypedDict):
    valid: bool
    language: Literal["python"]
    files_checked: int
    diagnostics: list[_DiagnosticRecord]


@dataclass(frozen=True)
class _Diagnostic:
    file: str
    code: str
    path: str
    message: str
    line: int | None = None
    column: int | None = None


def _pointer(parts: object) -> str:
    if not isinstance(parts, (list, tuple)):
        return ""
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def _schema_errors(value: object) -> list[ValidationError]:
    if isinstance(value, Mapping):
        version = value.get("version")
        if not isinstance(version, bool) and version in (1, 2):
            # Select the declared version to keep top-level oneOf branch noise out.
            validator = VALIDATOR.evolve(schema={"$ref": f"#/$defs/version{int(version)}"})
            errors = list(validator.iter_errors(value))
        else:
            return []
    else:
        errors = list(VALIDATOR.iter_errors(value))

    def expand(error: ValidationError) -> list[ValidationError]:
        if error.validator not in {"oneOf", "anyOf"} or not error.context:
            return [error]
        branches = list(error.context)
        if isinstance(error.instance, Mapping):
            # Prefer the branch identified by a discriminator-like property. This
            # avoids reporting mutually exclusive required fields for valueSource.
            matching = [
                branch
                for branch in branches
                if isinstance(branch.schema, Mapping)
                and isinstance(branch.schema.get("required"), list)
                and set(branch.schema["required"]).intersection(error.instance)
            ]
            if matching:
                branches = matching
            else:
                # With no recognized discriminator, an unknown-property error
                # identifies the useful issue without listing every mutually
                # exclusive required field from the alternatives.
                def unknown_fields(branch: ValidationError) -> list[ValidationError]:
                    if branch.validator == "additionalProperties":
                        return [branch]
                    return [leaf for child in branch.context for leaf in unknown_fields(child)]

                unknown = [leaf for branch in branches for leaf in unknown_fields(branch)]
                if unknown:
                    unknown.sort(
                        key=lambda leaf: (
                            len(leaf.absolute_path),
                            tuple(map(str, leaf.absolute_path)),
                        )
                    )
                    return [unknown[-1]]
            object_branches = [branch for branch in branches if branch.validator != "type"]
            if object_branches:
                branches = object_branches
        deepest = max(len(branch.absolute_path) for branch in branches)
        selected = [branch for branch in branches if len(branch.absolute_path) == deepest]
        return [leaf for branch in selected for leaf in expand(branch)]

    return [leaf for error in errors for leaf in expand(error)]


def _safe_schema_message(error: ValidationError) -> str:
    # jsonschema's message can include rejected config values; never surface it.
    validator_messages = {
        "required": "A required field is missing.",
        "additionalProperties": "The object contains an unsupported field.",
        "type": "The value has the wrong type.",
        "enum": "The value is not one of the supported choices.",
        "const": "The value does not match the required value.",
        "pattern": "The value has an invalid format.",
        "minLength": "The value is too short.",
        "minItems": "The list is too short.",
        "minimum": "The number is below the supported minimum.",
        "uniqueItems": "The list contains duplicate values.",
    }
    return validator_messages.get(
        error.validator, "The value does not satisfy the configuration schema."
    )


def _schema_diagnostics(file: Path, error: ValidationError) -> list[_Diagnostic]:
    path = list(error.absolute_path)
    if error.validator == "additionalProperties" and isinstance(error.instance, Mapping):
        properties = error.schema.get("properties", {})
        if isinstance(properties, Mapping):
            unexpected = sorted(set(error.instance) - set(properties), key=str)
            if unexpected:
                path.append(str(unexpected[0]))
    if error.validator == "required" and isinstance(error.instance, Mapping):
        required = error.validator_value
        if isinstance(required, list):
            missing = sorted((key for key in required if key not in error.instance), key=str)
            if missing:
                return [
                    _Diagnostic(
                        str(file.absolute()),
                        "schema",
                        _pointer((*path, str(key))),
                        "A required field is missing.",
                    )
                    for key in missing
                ]
    return [
        _Diagnostic(
            str(file.absolute()),
            "schema",
            _pointer(path),
            _safe_schema_message(error),
        )
    ]


def validate_instrumentation_source(source: str | Path) -> _ValidationReport:
    """Validate one instrumentation file or the immediate files in a directory."""
    path = Path(source).expanduser().absolute()
    diagnostics: list[_Diagnostic] = []
    selected: list[Path] = []
    if path.is_dir():
        try:
            selected = sorted(
                (
                    item
                    for item in path.iterdir()
                    if item.is_file() and item.suffix.lower() in {".json", ".yaml", ".yml"}
                ),
                key=lambda item: item.name,
            )
        except OSError:
            diagnostics.append(_Diagnostic(str(path), "io", "", "Directory could not be read."))
        else:
            if not selected:
                diagnostics.append(
                    _Diagnostic(str(path), "io", "", "No supported configuration files were found.")
                )
    elif path.is_file() and path.suffix.lower() in {".json", ".yaml", ".yml"}:
        selected = [path]
    else:
        diagnostics.append(
            _Diagnostic(str(path), "io", "", "Expected a JSON/YAML file or directory.")
        )

    files_checked = len(selected)
    stems: dict[str, Path] = {}
    for file in selected:
        try:
            if ensure_agent_name(file.stem) != file.stem:
                raise ValueError
        except ValueError:
            diagnostics.append(
                _Diagnostic(
                    str(file.absolute()), "identity", "", "Filename must use a valid agent name."
                )
            )
        else:
            if file.stem in stems:
                diagnostics.append(
                    _Diagnostic(
                        str(file.absolute()),
                        "identity",
                        "",
                        "Multiple files use the same agent name.",
                    )
                )
            else:
                stems[file.stem] = file
        try:
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            diagnostics.append(
                _Diagnostic(str(file.absolute()), "io", "", "File could not be read as UTF-8.")
            )
            continue
        fmt: Literal["json", "yaml"] = "json" if file.suffix.lower() == ".json" else "yaml"
        try:
            value = parse_document(text, fmt)
        except json.JSONDecodeError as exc:
            diagnostics.append(
                _Diagnostic(
                    str(file.absolute()),
                    "syntax",
                    "",
                    "Invalid JSON syntax.",
                    exc.lineno,
                    exc.colno,
                )
            )
            continue
        except InvalidInstrumentationRootError:
            diagnostics.append(
                _Diagnostic(
                    str(file.absolute()),
                    "schema",
                    "",
                    "The document root must be an object.",
                )
            )
            continue
        except Exception as exc:
            if isinstance(exc, LocatedParseError):
                diagnostics.append(
                    _Diagnostic(
                        str(file.absolute()),
                        "syntax",
                        "",
                        "Invalid JSON/YAML syntax or duplicate key.",
                        exc.line,
                        exc.column,
                    )
                )
                continue
            cause = exc.__cause__ or exc
            mark = getattr(cause, "problem_mark", None)
            line = getattr(exc, "line", None)
            column = getattr(exc, "column", None)
            if line is None:
                line = getattr(mark, "line", None)
                line = line + 1 if isinstance(line, int) else None
            if column is None:
                column = getattr(mark, "column", None)
                column = column + 1 if isinstance(column, int) else None
            diagnostics.append(
                _Diagnostic(
                    str(file.absolute()),
                    "syntax",
                    "",
                    "Invalid JSON/YAML syntax or duplicate key.",
                    line if isinstance(line, int) else None,
                    column if isinstance(column, int) else None,
                )
            )
            continue
        try:
            value = json_safe_config(value)
        except (TypeError, ValueError, RecursionError):
            diagnostics.append(
                _Diagnostic(
                    str(file.absolute()),
                    "schema",
                    "",
                    "The document contains values unsupported by JSON configuration.",
                )
            )
            continue
        schema_errors = _schema_errors(value)
        if isinstance(value, Mapping) and (
            isinstance(value.get("version"), bool) or value.get("version") not in (1, 2)
        ):
            diagnostics.append(
                _Diagnostic(
                    str(file.absolute()),
                    "schema",
                    "/version",
                    "The value does not satisfy the configuration schema.",
                )
            )
            continue
        if schema_errors:
            for error in schema_errors:
                diagnostics.extend(_schema_diagnostics(file, error))
            continue
        if isinstance(value, Mapping):
            checkpoints = value.get("checkpoints")
            if isinstance(checkpoints, Mapping):
                invalid_state = False
                for name, checkpoint in checkpoints.items():
                    if (
                        isinstance(checkpoint, Mapping)
                        and checkpoint.get("input_from") == "all_params"
                    ):
                        diagnostics.append(
                            _Diagnostic(
                                str(file.absolute()),
                                "capability",
                                _pointer(("checkpoints", str(name), "input_from")),
                                "The Python SDK does not support input_from all_params.",
                            )
                        )
                    if isinstance(checkpoint, Mapping) and "input_schema" in checkpoint:
                        schema_path = _pointer(("checkpoints", str(name), "input_schema"))
                        try:
                            compiled = compile_input_schema(checkpoint["input_schema"])
                        except InputSchemaError:
                            diagnostics.append(
                                _Diagnostic(
                                    str(file.absolute()),
                                    "schema",
                                    schema_path,
                                    "Checkpoint input_schema is unsupported or invalid.",
                                )
                            )
                        else:
                            source_value = checkpoint.get("input_from")
                            if isinstance(source_value, Mapping) and "value" in source_value:
                                instance_path = compiled.validate(source_value["value"])
                                if instance_path is not None:
                                    diagnostics.append(
                                        _Diagnostic(
                                            str(file.absolute()),
                                            "schema",
                                            _pointer(
                                                ("checkpoints", str(name), "input_from", "value")
                                            ),
                                            "Literal checkpoint input does not satisfy "
                                            "input_schema "
                                            f"(instance at {instance_path or '/'}).",
                                        )
                                    )
                    if not isinstance(checkpoint, Mapping) or "state" not in checkpoint:
                        continue
                    try:
                        copy_observed_state(checkpoint["state"])
                    except (TypeError, ValueError, RecursionError):
                        diagnostics.append(
                            _Diagnostic(
                                str(file.absolute()),
                                "schema",
                                _pointer(("checkpoints", str(name), "state")),
                                (
                                    "Checkpoint state exceeds runtime limits or contains "
                                    "unsupported JSON values."
                                ),
                            )
                        )
                        invalid_state = True
                if invalid_state:
                    continue
            try:
                normalize_turn_aliases(dict(value))
            except (TypeError, ValueError, RecursionError):
                diagnostics.append(
                    _Diagnostic(
                        str(file.absolute()),
                        "schema",
                        "",
                        "The document contains values or combinations unsupported by the schema.",
                    )
                )

            def check_selector(
                source: object,
                parts: tuple[str, ...],
                unavailable: frozenset[str] = frozenset({"result", "error"}),
                allowed: frozenset[str] | None = None,
            ) -> None:
                if not isinstance(source, Mapping):
                    return
                selector = source.get("path")
                if not isinstance(selector, list) or not selector:
                    return
                root = selector[0]
                if root in unavailable or (allowed is not None and root not in allowed):
                    diagnostics.append(
                        _Diagnostic(
                            str(file.absolute()),
                            "selector",
                            _pointer((*parts, "path")),
                            "This selector is unavailable before the operation returns.",
                        )
                    )

            entrypoints = value.get("entrypoints")
            if value.get("version") == 2 and isinstance(entrypoints, list):
                for index, entrypoint in enumerate(entrypoints):
                    if not isinstance(entrypoint, Mapping):
                        continue
                    if "parameter_names" in entrypoint:
                        diagnostics.append(
                            _Diagnostic(
                                str(file.absolute()),
                                "capability",
                                _pointer(("entrypoints", str(index), "parameter_names")),
                                "Python derives target parameters from the callable signature.",
                            )
                        )
                    for field in ("context",):
                        context_source = entrypoint.get(field)
                        check_selector(
                            context_source,
                            ("entrypoints", str(index), field),
                            unavailable=frozenset({"result", "error", "context"}),
                        )
                    for group in ("target_scope", "trace"):
                        sources = entrypoint.get(group)
                        if isinstance(sources, Mapping):
                            for field, source in sources.items():
                                check_selector(
                                    source, ("entrypoints", str(index), group, str(field))
                                )
                    turn = entrypoint.get("turn")
                    if isinstance(turn, Mapping):
                        for field in ("session_id", "turn_id", "user_request"):
                            check_selector(
                                turn.get(field), ("entrypoints", str(index), "turn", field)
                            )
            checkpoints = value.get("checkpoints")
            if isinstance(checkpoints, Mapping):
                for name, checkpoint in checkpoints.items():
                    if not isinstance(checkpoint, Mapping):
                        continue
                    for field in (
                        "state_from",
                        "input_from",
                        "context_from",
                        "tools_from",
                        "ground_truth_from",
                    ):
                        check_selector(checkpoint.get(field), ("checkpoints", str(name), field))
                    action = checkpoint.get("action")
                    if isinstance(action, Mapping):
                        for field in ("target", "command", "cwd"):
                            check_selector(
                                action.get(field),
                                ("checkpoints", str(name), "action", field),
                            )
            operations = value.get("operations")
            if isinstance(operations, Mapping):
                for name, operation in operations.items():
                    if not isinstance(operation, Mapping):
                        continue
                    for field in ("parameter_names", "returns"):
                        if field in operation:
                            diagnostics.append(
                                _Diagnostic(
                                    str(file.absolute()),
                                    "capability",
                                    _pointer(("operations", str(name), field)),
                                    "This operation option is not supported by the Python SDK.",
                                )
                            )
                    for field in ("operation_key", "input", "context", "evidence", "tools"):
                        check_selector(operation.get(field), ("operations", str(name), field))
                    action = operation.get("action")
                    if isinstance(action, Mapping):
                        for field in ("target", "command"):
                            check_selector(
                                action.get(field), ("operations", str(name), "action", field)
                            )
                    observations = operation.get("observations")
                    if isinstance(observations, Mapping):
                        for stage, fields in observations.items():
                            if isinstance(fields, Mapping):
                                for field, source in fields.items():
                                    check_selector(
                                        source,
                                        (
                                            "operations",
                                            str(name),
                                            "observations",
                                            str(stage),
                                            str(field),
                                        ),
                                        unavailable=frozenset(),
                                        allowed={
                                            "attempted": frozenset(
                                                {"args", "kwargs", "context", "input", "params"}
                                            ),
                                            "succeeded": frozenset(
                                                {
                                                    "args",
                                                    "kwargs",
                                                    "context",
                                                    "input",
                                                    "params",
                                                    "result",
                                                }
                                            ),
                                            "failed": frozenset(
                                                {
                                                    "args",
                                                    "kwargs",
                                                    "context",
                                                    "input",
                                                    "params",
                                                    "error",
                                                }
                                            ),
                                        }.get(str(stage), frozenset()),
                                    )
            checkpoints = value.get("checkpoints")
            if isinstance(checkpoints, Mapping):
                for name, checkpoint in checkpoints.items():
                    if not isinstance(checkpoint, Mapping):
                        continue
                    prefix = ("checkpoints", str(name))
                    if (
                        "parameter_names" in checkpoint
                        and checkpoint.get("input_from") != "all_params"
                    ):
                        diagnostics.append(
                            _Diagnostic(
                                str(file.absolute()),
                                "capability",
                                _pointer((*prefix, "parameter_names")),
                                "Python derives target parameters from the callable signature.",
                            )
                        )
                    if value.get("version") != 2 and isinstance(checkpoint.get("options"), Mapping):
                        diagnostics.append(
                            _Diagnostic(
                                str(file.absolute()),
                                "capability",
                                _pointer((*prefix, "options")),
                                "Inline checkpoint options require instrumentation version 2.",
                            )
                        )
                    inline_options = checkpoint.get("options")
                    if isinstance(inline_options, Mapping):
                        for option in sorted(
                            set(inline_options)
                            & {"client", "stepName", "stepType", "targetType", "targetId"}
                        ):
                            diagnostics.append(
                                _Diagnostic(
                                    str(file.absolute()),
                                    "capability",
                                    _pointer((*prefix, "options", str(option))),
                                    "This inline option is not supported by the Python SDK.",
                                )
                            )
            connection = value.get("connection")
            if isinstance(connection, Mapping):
                for unsupported in sorted(
                    set(connection) & {"http_client", "debug_logger", "user_agent"}
                ):
                    diagnostics.append(
                        _Diagnostic(
                            str(file.absolute()),
                            "capability",
                            "/connection/" + str(unsupported),
                            "This connection option is not supported by the Python SDK.",
                        )
                    )
            adapters = value.get("adapters")
            if isinstance(adapters, Mapping):
                for adapter_name, adapter in adapters.items():
                    if not isinstance(adapter, Mapping) or adapter.get("kind") != "openai_agents":
                        continue
                    tools = adapter.get("tools")
                    if not isinstance(tools, Mapping):
                        continue
                    for tool_name, tool_config in tools.items():
                        if not isinstance(tool_config, Mapping):
                            continue
                        tool_prefix = (
                            "adapters",
                            str(adapter_name),
                            "tools",
                            str(tool_name),
                        )
                        if "tool" not in tool_config:
                            diagnostics.append(
                                _Diagnostic(
                                    str(file.absolute()),
                                    "capability",
                                    _pointer(tool_prefix),
                                    "Python requires a native OpenAI FunctionTool binding.",
                                )
                            )
                        action = tool_config.get("action")
                        if isinstance(action, Mapping):
                            for field in ("target", "command"):
                                check_selector(
                                    action.get(field),
                                    (*tool_prefix, "action", field),
                                    unavailable=frozenset({"result", "error", "params"}),
                                )
    diagnostics.sort(key=lambda item: (item.file, item.path, item.code, item.message))
    diagnostics = list(dict.fromkeys(diagnostics))
    return {
        "valid": not diagnostics,
        "language": "python",
        "files_checked": files_checked,
        "diagnostics": [
            {
                "file": item.file,
                "code": item.code,
                "path": item.path,
                "message": item.message,
                "line": item.line,
                "column": item.column,
            }
            for item in diagnostics
        ],
    }
