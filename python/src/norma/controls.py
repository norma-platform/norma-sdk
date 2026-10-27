"""Pure, conservative helpers for authoring Console rule controls.

This module has no network or hosted-runtime dependencies. Server preparation
remains authoritative for full evaluator and cross-field validation.
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Required, TypedDict, cast

type ControlType = Literal["number", "boolean", "string", "enum", "string_list"]
type ControlStatus = Literal["valid", "invalid", "requires_server_validation"]
type SourceValidation = Literal["admitted", "invalid", "unavailable"]
type ControlApplicabilityOperator = Literal["present", "nonempty", "has_string_enum", "not_false"]


class ControlExpectedValueSchema(TypedDict, total=False):
    """Trusted constraints for an evaluator destination."""

    types: Required[list[ControlType]]
    minimum: int | float | None
    maximum: int | float | None
    integer: bool | None
    min_length: int | None
    max_length: int | None
    min_items: int | None
    max_items: int | None
    item_max_length: int | None
    allowed_values: list[str] | None


class ControlApplicabilityPredicate(TypedDict):
    """Typed structural operator used by the trusted capability projection."""

    path_template: str
    operator: ControlApplicabilityOperator


class ControlSlotDescriptor(TypedDict):
    """A reviewed slot in a trusted evaluator manifest."""

    evaluator: str
    evaluator_version: str
    schema_fingerprint: str
    path_template: str
    control_types: list[ControlType]
    predicates: list[list[object]]
    applicability: list[dict[str, str]]
    affects: Literal["decision", "assessment"]
    expected_value_schema: ControlExpectedValueSchema
    requires_server_validation: bool


class ControlEvaluatorManifest(TypedDict):
    """Trusted slot registry for one evaluator version."""

    evaluator: str
    evaluator_version: str
    schema_fingerprint: str
    slots: list[ControlSlotDescriptor]


class TrustedControlCapabilities(TypedDict):
    """Explicit server projection; never inferred from authored rule data."""

    contract: Literal["rule-controls-v1"]
    fingerprint: str
    manifests: list[ControlEvaluatorManifest]


class _DeclarationBase(TypedDict, total=False):
    description: str


class NumberControlDeclaration(_DeclarationBase, total=False):
    type: Required[Literal["number"]]
    label: Required[str]
    default: Required[int | float]
    minimum: Required[int | float]
    maximum: Required[int | float]
    integer: bool


class BooleanControlDeclaration(_DeclarationBase, total=False):
    type: Required[Literal["boolean"]]
    label: Required[str]
    default: Required[bool]


class StringControlDeclaration(_DeclarationBase, total=False):
    type: Required[Literal["string"]]
    label: Required[str]
    default: Required[str]
    min_length: int | None
    max_length: int | None


class EnumControlDeclaration(_DeclarationBase, total=False):
    type: Required[Literal["enum"]]
    label: Required[str]
    default: Required[str]
    allowed_values: Required[list[str]]


class StringListControlDeclaration(_DeclarationBase, total=False):
    type: Required[Literal["string_list"]]
    label: Required[str]
    default: Required[list[str]]
    allowed_values: list[str] | None
    min_items: int | None
    max_items: int | None


type ControlDeclaration = (
    NumberControlDeclaration
    | BooleanControlDeclaration
    | StringControlDeclaration
    | EnumControlDeclaration
    | StringListControlDeclaration
)


_CONTRACT = "rule-controls-v1"
_MAX_SOURCE_BYTES = 1_048_576
_MAX_DEPTH = 64
_MAX_NODES = 10_000
_MAX_REFERENCES = 256
_MAX_DIAGNOSTICS = 50
_MAX_EFFECTIVE_BYTES = 16_384
_MAX_STRING_SCALARS = 4_096
_MAX_CONTROLS = 32
_MAX_LIST_ITEMS = 128
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_FORBIDDEN_KEYS = {"__proto__", "prototype", "constructor"}
_CONTROL_TYPES = {"number", "boolean", "string", "enum", "string_list"}
_PREDICATES = {"present", "nonempty", "has_string_enum", "not_false"}


@dataclass(frozen=True, slots=True)
class ControlDiagnostic:
    """One bounded issue with an RFC 6901 authored-source pointer."""

    code: str
    source_pointer: str
    message: str


@dataclass(frozen=True, slots=True)
class ControlValidationResult:
    """Immutable local validation result; not a server preparation receipt."""

    status: ControlStatus
    source_validation: SourceValidation
    requires_server_validation: bool
    diagnostics: tuple[ControlDiagnostic, ...]
    authored_document: Mapping[str, object] | None
    references: tuple[ControlReference, ...] = ()


@dataclass(frozen=True, slots=True)
class ControlReference:
    key: str
    source_pointer: str
    destination_pointer: str | None
    evaluator_pointer: str | None


@dataclass(frozen=True, slots=True)
class _ReferenceSite:
    pointer: str
    key: str


@dataclass(frozen=True, slots=True)
class _Evaluator:
    pointer: str
    name: str
    config: Mapping[str, object]


class _DuplicateKey(ValueError):
    pass


class _InvalidConstant(ValueError):
    pass


def validate_authored_controls(
    source: str | Mapping[str, object],
    trusted_capabilities: Mapping[str, object] | None,
) -> ControlValidationResult:
    """Check raw/parsed authored source against explicitly supplied capabilities.

    Text input receives duplicate-aware strict JSON admission. Mapping input
    cannot recover duplicate keys or number spellings and therefore always
    requires server validation. Unknown manifests or predicate operators never
    grant a destination. Known local violations are still returned as invalid.
    """
    diagnostics: list[ControlDiagnostic] = []
    source_validation: SourceValidation = "admitted"
    if isinstance(source, str):
        try:
            raw_size = len(source.encode("utf-8", errors="strict"))
        except UnicodeEncodeError:
            return _result(
                "invalid",
                "invalid",
                False,
                [_diag("control_source_invalid_unicode", "", "Source contains invalid Unicode.")],
            )
        if raw_size > _MAX_SOURCE_BYTES:
            return _result(
                "invalid",
                "invalid",
                False,
                [_diag("control_source_too_large", "", "Source exceeds the 1 MiB limit.")],
            )
        try:
            document = json.loads(
                source, object_pairs_hook=_object_no_duplicates, parse_constant=_reject_constant
            )
        except _DuplicateKey:
            return _result(
                "invalid",
                "invalid",
                False,
                [
                    _diag(
                        "control_source_duplicate_key",
                        "",
                        "Source contains a duplicate object key.",
                    )
                ],
            )
        except (json.JSONDecodeError, _InvalidConstant, RecursionError, ValueError):
            return _result(
                "invalid",
                "invalid",
                False,
                [_diag("control_source_invalid_json", "", "Source is not strict JSON.")],
            )
    elif type(source) is dict:
        source_validation = "unavailable"
        try:
            document = _copy_json(source)
            serialized_source = json.dumps(
                document, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
            if len(serialized_source) > _MAX_SOURCE_BYTES:
                raise ValueError("Parsed source exceeds the source byte limit.")
        except (TypeError, ValueError, OverflowError):
            return _result(
                "invalid",
                "invalid",
                False,
                [_diag("control_source_invalid_json", "", "Parsed source is not strict JSON.")],
            )
        diagnostics.append(
            _diag(
                "control_source_admission_unavailable",
                "",
                "Duplicate keys and original number spellings cannot be checked after parsing.",
            )
        )
    else:
        return _result(
            "invalid",
            "invalid",
            False,
            [
                _diag(
                    "control_source_invalid_json",
                    "",
                    "Source must be raw JSON text or a parsed object.",
                )
            ],
        )

    if not isinstance(document, dict):
        return _result(
            "invalid",
            source_validation,
            False,
            diagnostics
            + [
                _diag("control_source_invalid_shape", "", "Authored rule source must be an object.")
            ],
        )
    try:
        _check_tree_bounds(document)
    except ValueError as exc:
        code = (
            "control_source_bounds_exceeded"
            if str(exc) == "Complexity bound exceeded."
            else "control_source_invalid_json"
        )
        return _result(
            "invalid",
            "invalid",
            False,
            diagnostics + [_diag(code, "", "Source is outside strict JSON bounds.")],
        )

    retired_fields = {"template", "template_values", "parameters"} & document.keys()
    if retired_fields:
        field = sorted(retired_fields)[0]
        return _result(
            "invalid",
            source_validation,
            False,
            [_diag("control_declaration_invalid", f"/{_escape(field)}", "Unsupported Rule field.")],
            document,
        )

    raw_controls = document.get("controls")
    if "controls" in document and not isinstance(raw_controls, dict):
        diagnostics.append(
            _diag("control_declaration_invalid", "/controls", "Root controls must be an object.")
        )
        return _result("invalid", source_validation, False, diagnostics, document)
    declarations = raw_controls if isinstance(raw_controls, dict) else {}
    if len(declarations) > _MAX_CONTROLS:
        diagnostics.append(
            _diag(
                "control_declaration_invalid", "/controls", "At most 32 declarations are supported."
            )
        )
    for key, declaration in declarations.items():
        _validate_declaration(key, declaration, diagnostics)

    references, marker_error = _references(document)
    if marker_error:
        diagnostics.append(marker_error)
    if ("controls" in document or references) and document.get("execution") != "server":
        diagnostics.append(
            _diag(
                "control_execution_unsupported",
                "/execution",
                "Authored controls require server execution.",
            )
        )
    if len(references) > _MAX_REFERENCES:
        diagnostics.append(
            _diag(
                "control_declaration_invalid", "/", "At most 256 control references are supported."
            )
        )
    contexts = _evaluators(document)
    requires_server = source_validation == "unavailable"
    source_valid_references: list[_ReferenceSite] = []
    for reference in references:
        if reference.key not in declarations:
            diagnostics.append(
                _diag(
                    "control_reference_unknown",
                    reference.pointer,
                    "Reference names no declared control.",
                )
            )
            continue
        if not any(reference.pointer.startswith(item.pointer + "/config/") for item in contexts):
            diagnostics.append(
                _diag(
                    "control_reference_not_allowed",
                    reference.pointer,
                    "Reference is outside a supported evaluator configuration.",
                )
            )
            continue
        source_valid_references.append(reference)

    referenced_keys = {item.key for item in references}
    for key in declarations:
        if key not in referenced_keys:
            diagnostics.append(
                _diag(
                    "control_unused",
                    f"/controls/{_escape(key)}",
                    "Declared control is not used by a reference.",
                )
            )

    if source_valid_references:
        if (
            not isinstance(trusted_capabilities, Mapping)
            or trusted_capabilities.get("contract") != _CONTRACT
        ):
            diagnostics.append(
                _diag(
                    "control_capability_unavailable",
                    "/",
                    "Trusted rule-controls-v1 capabilities were not supplied.",
                )
            )
            requires_server = True
        else:
            manifests = trusted_capabilities.get("manifests")
            capability_fingerprint = trusted_capabilities.get("fingerprint")
            if (
                not isinstance(capability_fingerprint, str)
                or not re.fullmatch(r"[a-f0-9]{64}", capability_fingerprint)
                or not isinstance(manifests, Sequence)
                or isinstance(manifests, (str, bytes))
                or len(manifests) > 256
                or any(not _manifest_shape_valid(item) for item in manifests)
            ):
                diagnostics.append(
                    _diag(
                        "control_capability_unavailable",
                        "/",
                        "Trusted manifest list is unavailable.",
                    )
                )
                requires_server = True
            else:
                for reference in source_valid_references[:_MAX_REFERENCES]:
                    requires_server |= _check_reference(
                        reference, declarations, contexts, manifests, diagnostics
                    )

    effective_values = {
        key: declaration["default"]
        for key, declaration in declarations.items()
        if isinstance(declaration, Mapping) and "default" in declaration
    }
    if any(type(value) is float for value in _walk(effective_values)):
        requires_server = True
        diagnostics.append(
            _diag(
                "control_serialization_requires_server_validation",
                "/controls",
                "Floating-point value serialization must be checked by server preparation.",
            )
        )
    try:
        effective_bytes = json.dumps(
            effective_values, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, OverflowError):
        requires_server = True
        diagnostics.append(
            _diag(
                "control_serialization_requires_server_validation",
                "/",
                "Effective-value serialization must be checked by server preparation.",
            )
        )
    else:
        if len(effective_bytes) > _MAX_EFFECTIVE_BYTES:
            diagnostics.append(
                _diag(
                    "control_declaration_invalid",
                    "/controls",
                    "Effective control values exceed 16 KiB.",
                )
            )

    diagnostics = _bounded_diagnostics(diagnostics)
    invalid_codes = {
        "control_source_admission_unavailable",
        "control_capability_unavailable",
        "control_serialization_requires_server_validation",
    }
    invalid = any(item.code not in invalid_codes for item in diagnostics)
    status: ControlStatus = (
        "invalid" if invalid else "requires_server_validation" if requires_server else "valid"
    )
    return ControlValidationResult(
        status,
        source_validation,
        requires_server,
        tuple(diagnostics),
        cast(Mapping[str, object], _freeze(document)),
        tuple(
            ControlReference(
                key=reference.key,
                source_pointer=reference.pointer,
                destination_pointer=reference.pointer
                if any(
                    reference.pointer.startswith(item.pointer + "/config/")
                    for item in contexts
                )
                else None,
                evaluator_pointer=next(
                    (
                        item.pointer
                        for item in contexts
                        if reference.pointer.startswith(item.pointer + "/config/")
                    ),
                    None,
                ),
            )
            for reference in references[:_MAX_REFERENCES]
        ),
    )


def _object_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise _InvalidConstant(value)


def _diag(code: str, pointer: str, message: str) -> ControlDiagnostic:
    return ControlDiagnostic(code[:96], pointer[:1024], message[:256])


def _result(
    status: ControlStatus,
    source: SourceValidation,
    requires: bool,
    diagnostics: list[ControlDiagnostic],
    document: Mapping[str, object] | None = None,
) -> ControlValidationResult:
    return ControlValidationResult(
        status,
        source,
        requires,
        tuple(_bounded_diagnostics(diagnostics)),
        cast(Mapping[str, object], _freeze(document)) if document is not None else None,
    )


def _bounded_diagnostics(items: list[ControlDiagnostic]) -> list[ControlDiagnostic]:
    if len(items) <= _MAX_DIAGNOSTICS:
        return items
    return items[: _MAX_DIAGNOSTICS - 1] + [
        _diag("control_diagnostics_truncated", "/", "Additional diagnostics were omitted.")
    ]


def _copy_json(
    value: object,
    depth: int = 0,
    count: list[int] | None = None,
    byte_hint: list[int] | None = None,
) -> object:
    if count is None:
        count = [0]
    if byte_hint is None:
        byte_hint = [0]
    count[0] += 1
    if count[0] > _MAX_NODES or depth > _MAX_DEPTH:
        raise ValueError("JSON complexity exceeds bounds.")
    if value is None or type(value) in (bool, str, int):
        if isinstance(value, str) and not _unicode_ok(value):
            raise ValueError("Invalid Unicode.")
        if isinstance(value, str):
            if len(value) > _MAX_SOURCE_BYTES:
                raise ValueError("Source string exceeds bounds.")
            byte_hint[0] += len(value.encode("utf-8"))
        if type(value) is int and abs(value) > int(sys.float_info.max):
            raise ValueError("JSON integer exceeds the finite number range.")
        if type(value) is int:
            byte_hint[0] += len(str(value))
        elif value is None:
            byte_hint[0] += 4
        elif type(value) is bool:
            byte_hint[0] += 5
        if byte_hint[0] > _MAX_SOURCE_BYTES:
            raise ValueError("Source exceeds bounds.")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Non-finite number.")
        byte_hint[0] += len(repr(value))
        if byte_hint[0] > _MAX_SOURCE_BYTES:
            raise ValueError("Source exceeds bounds.")
        return value
    if type(value) is dict:
        byte_hint[0] += 2
        copied: dict[str, object] = {}
        for key, child in value.items():
            if not isinstance(key, str) or not _unicode_ok(key):
                raise ValueError("JSON keys must be valid Unicode strings.")
            if len(key) > _MAX_SOURCE_BYTES:
                raise ValueError("Source key exceeds bounds.")
            byte_hint[0] += len(json.dumps(key, ensure_ascii=False).encode("utf-8")) + 1
            if byte_hint[0] > _MAX_SOURCE_BYTES:
                raise ValueError("Source exceeds bounds.")
            copied[key] = _copy_json(child, depth + 1, count, byte_hint)
        return copied
    if type(value) is list:
        byte_hint[0] += 2
        if byte_hint[0] > _MAX_SOURCE_BYTES:
            raise ValueError("Source exceeds bounds.")
        return [_copy_json(child, depth + 1, count, byte_hint) for child in value]
    raise TypeError("Expected JSON value.")


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    return value


def _unicode_ok(value: str) -> bool:
    return all(not 0xD800 <= ord(char) <= 0xDFFF for char in value)


def _check_tree_bounds(document: object) -> None:
    pending: list[tuple[object, int]] = [(document, 0)]
    nodes = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if nodes > _MAX_NODES or depth > _MAX_DEPTH:
            raise ValueError("Complexity bound exceeded.")
        if isinstance(value, str) and not _unicode_ok(value):
            raise ValueError("Invalid Unicode.")
        if type(value) is int and abs(value) > int(sys.float_info.max):
            raise ValueError("JSON integer exceeds the finite number range.")
        if type(value) is float and not math.isfinite(value):
            raise ValueError("JSON number is not finite.")
        if isinstance(value, dict):
            if any(not _unicode_ok(key) for key in value):
                raise ValueError("Invalid Unicode key.")
            pending.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)


def _escape(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _references(
    document: Mapping[str, object],
) -> tuple[list[_ReferenceSite], ControlDiagnostic | None]:
    pending: list[tuple[object, str]] = [(document, "")]
    references: list[_ReferenceSite] = []
    while pending and len(references) <= _MAX_REFERENCES:
        value, pointer = pending.pop()
        if isinstance(value, Mapping):
            if "$param" in value:
                raw_key = value.get("$param")
                if len(value) != 1 or not isinstance(raw_key, str):
                    return references, _diag(
                        "control_reference_invalid",
                        pointer or "/",
                        "Reserved reference must be exactly an object containing a string $param.",
                    )
                references.append(_ReferenceSite(pointer or "/", raw_key))
                continue
            for key, child in value.items():
                if pointer == "" and key == "controls":
                    continue
                pending.append((child, f"{pointer}/{_escape(str(key))}"))
        elif isinstance(value, list):
            pending.extend((child, f"{pointer}/{index}") for index, child in enumerate(value))
    return references, None


def _evaluators(document: Mapping[str, object]) -> list[_Evaluator]:
    root = document
    root_pointer = ""
    roots: list[tuple[object, str]]
    if isinstance(root, Mapping) and "condition" in root:
        roots = [(root["condition"], root_pointer + "/condition")]
    elif isinstance(root, Mapping) and "selector" in root and "evaluator" in root:
        roots = [(root, root_pointer)]
    else:
        roots = []
    found: list[_Evaluator] = []

    def visit(node: object, pointer: str, depth: int) -> None:
        if depth > _MAX_DEPTH or not isinstance(node, Mapping):
            return
        composite = [key for key in ("and", "or", "not") if key in node]
        if "selector" in node and "evaluator" in node and not composite:
            evaluator = node.get("evaluator")
            if (
                isinstance(evaluator, Mapping)
                and isinstance(evaluator.get("name"), str)
                and isinstance(evaluator.get("config"), Mapping)
            ):
                found.append(
                    _Evaluator(pointer + "/evaluator", evaluator["name"], evaluator["config"])
                )
            return
        if len(composite) != 1 or "selector" in node or "evaluator" in node:
            return
        operator = composite[0]
        child = node[operator]
        if operator in ("and", "or") and isinstance(child, list) and child:
            for index, item in enumerate(child):
                visit(item, f"{pointer}/{operator}/{index}", depth + 1)
        elif operator == "not" and isinstance(child, Mapping):
            visit(child, f"{pointer}/not", depth + 1)

    for root_node, pointer in roots:
        visit(root_node, pointer, 0)
    return found


def _check_reference(
    reference: _ReferenceSite,
    declarations: Mapping[str, object],
    evaluators: Sequence[_Evaluator],
    manifests: Sequence[object],
    diagnostics: list[ControlDiagnostic],
) -> bool:
    if reference.key not in declarations:
        diagnostics.append(
            _diag(
                "control_reference_unknown",
                reference.pointer,
                "Reference names no declared control.",
            )
        )
        return False
    evaluator = next(
        (item for item in evaluators if reference.pointer.startswith(item.pointer + "/config/")),
        None,
    )
    if evaluator is None:
        diagnostics.append(
            _diag(
                "control_reference_not_allowed",
                reference.pointer,
                "Reference is outside a supported evaluator configuration.",
            )
        )
        return False
    manifest = next(
        (
            item
            for item in manifests
            if isinstance(item, Mapping) and item.get("evaluator") == evaluator.name
        ),
        None,
    )
    if manifest is None:
        diagnostics.append(
            _diag(
                "control_capability_unavailable",
                reference.pointer,
                "No trusted manifest is available for this evaluator.",
            )
        )
        return True
    if (
        not isinstance(manifest.get("evaluator_version"), str)
        or not isinstance(manifest.get("schema_fingerprint"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", manifest["schema_fingerprint"])
    ):
        diagnostics.append(
            _diag(
                "control_capability_unavailable",
                reference.pointer,
                "Evaluator manifest identity is malformed.",
            )
        )
        return True
    slots = manifest.get("slots")
    if not isinstance(slots, Sequence) or isinstance(slots, (str, bytes)):
        diagnostics.append(
            _diag(
                "control_capability_unavailable",
                reference.pointer,
                "Evaluator slot metadata is malformed.",
            )
        )
        return True
    config_pointer = evaluator.pointer + "/config"
    actual_path = "/config" + reference.pointer[len(config_pointer) :]
    unknown_predicate = False
    for slot in slots:
        if not isinstance(slot, Mapping) or not isinstance(slot.get("path_template"), str):
            unknown_predicate = True
            continue
        matched_path, captures = _match_path(slot["path_template"], actual_path)
        if not matched_path:
            continue
        allowed, unknown = _matches_predicates(slot, evaluator.config, actual_path, captures)
        unknown_predicate |= unknown
        if not allowed:
            continue
        if not _declaration_fits_slot(declarations[reference.key], slot):
            diagnostics.append(
                _diag(
                    "control_destination_invalid",
                    reference.pointer,
                    "Declaration does not satisfy the trusted destination value schema.",
                )
            )
        return slot.get("requires_server_validation") is not False
    if unknown_predicate:
        diagnostics.append(
            _diag(
                "control_capability_unavailable",
                reference.pointer,
                "A trusted applicability predicate is unknown or malformed.",
            )
        )
        return True
    diagnostics.append(
        _diag(
            "control_reference_not_allowed",
            reference.pointer,
            "No trusted control slot allows this destination.",
        )
    )
    return False


def _manifest_shape_valid(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    evaluator = value.get("evaluator")
    version = value.get("evaluator_version")
    fingerprint = value.get("schema_fingerprint")
    slots = value.get("slots")
    if (
        not isinstance(evaluator, str)
        or not evaluator
        or not isinstance(version, str)
        or not version
        or not isinstance(fingerprint, str)
        or not re.fullmatch(r"[a-f0-9]{64}", fingerprint)
        or not isinstance(slots, Sequence)
        or isinstance(slots, (str, bytes))
        or len(slots) > 512
    ):
        return False
    for slot in slots:
        if not isinstance(slot, Mapping):
            return False
        schema = slot.get("expected_value_schema")
        types = slot.get("control_types")
        if (
            slot.get("evaluator") != evaluator
            or slot.get("evaluator_version") != version
            or slot.get("schema_fingerprint") != fingerprint
            or not isinstance(slot.get("path_template"), str)
            or not slot["path_template"].startswith("/config/")
            or not isinstance(types, Sequence)
            or isinstance(types, (str, bytes))
            or not types
            or any(not isinstance(kind, str) or kind not in _CONTROL_TYPES for kind in types)
            or not isinstance(slot.get("affects"), str)
            or slot.get("affects") not in {"decision", "assessment"}
            or type(slot.get("requires_server_validation")) is not bool
            or not isinstance(slot.get("predicates", []), Sequence)
            or not isinstance(slot.get("applicability", []), Sequence)
            or not isinstance(schema, Mapping)
            or schema.get("types") != list(types)
        ):
            return False
        if any(
            key in schema and schema[key] is not None and not _finite_number(schema[key])
            for key in ("minimum", "maximum")
        ):
            return False
        if (
            _finite_number(schema.get("minimum"))
            and _finite_number(schema.get("maximum"))
            and cast(int | float, schema["minimum"]) > cast(int | float, schema["maximum"])
        ):
            return False
        if schema.get("integer") is not None and type(schema["integer"]) is not bool:
            return False
        if any(
            key in schema and schema[key] is not None and not _bounded_int(schema[key], limit)
            for key, limit in (
                ("min_length", _MAX_STRING_SCALARS),
                ("max_length", 16_384),
                ("min_items", _MAX_LIST_ITEMS),
                ("max_items", _MAX_LIST_ITEMS),
                ("item_max_length", _MAX_STRING_SCALARS),
            )
        ):
            return False
        for minimum_key, maximum_key in (
            ("min_length", "max_length"),
            ("min_items", "max_items"),
        ):
            minimum_value, maximum_value = schema.get(minimum_key), schema.get(maximum_key)
            if (
                minimum_value is not None
                and maximum_value is not None
                and _bounded_int(minimum_value, _MAX_LIST_ITEMS * 32)
                and _bounded_int(maximum_value, _MAX_LIST_ITEMS * 128)
                and minimum_value > maximum_value
            ):
                return False
        allowed = schema.get("allowed_values")
        if allowed is not None and (
            not isinstance(allowed, Sequence)
            or isinstance(allowed, (str, bytes))
            or any(not isinstance(item, str) for item in allowed)
        ):
            return False
    return True


def _match_path(template: str, actual: str) -> tuple[bool, dict[str, str]]:
    expected_parts = template.split("/")[1:]
    actual_parts = actual.split("/")[1:]
    if len(expected_parts) != len(actual_parts):
        return False, {}
    captures: dict[str, str] = {}
    for index, (expected, actual_part) in enumerate(zip(expected_parts, actual_parts, strict=True)):
        if expected == "*":
            captures[str(index)] = actual_part
        elif expected != actual_part:
            return False, {}
    return True, captures


def _wildcard_path(template: str, actual: str) -> str:
    expected_parts = template.split("/")[1:]
    actual_parts = actual.split("/")[1:]
    return "/" + "/".join(
        actual_parts[index] if part == "*" and index < len(actual_parts) else part
        for index, part in enumerate(expected_parts)
    )


def _pointer_value(root: object, pointer: str) -> tuple[bool, object]:
    value = root
    for part in pointer.split("/")[1:]:
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, Mapping) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return False, None
    return True, value


def _matches_predicates(
    slot: Mapping[str, object],
    config: Mapping[str, object],
    actual_path: str,
    captures: Mapping[str, str],
) -> tuple[bool, bool]:
    del captures  # Captures are represented by the concrete path, not a second pattern language.
    literal = slot.get("predicates", [])
    if not isinstance(literal, Sequence) or isinstance(literal, (str, bytes)):
        return False, True
    for predicate in literal:
        if (
            not isinstance(predicate, Sequence)
            or isinstance(predicate, (str, bytes))
            or len(predicate) != 2
            or not isinstance(predicate[0], str)
        ):
            return False, True
        path = _wildcard_path(predicate[0], actual_path).removeprefix("/config")
        found, value = _pointer_value(config, path)
        if not found:
            if predicate[1] is None:
                continue
            return False, False
        if (
            _contains_marker(value)
            or type(value) is not type(predicate[1])
            or value != predicate[1]
        ):
            return False, False
    structural = slot.get("applicability", [])
    if not isinstance(structural, Sequence) or isinstance(structural, (str, bytes)):
        return False, True
    for predicate in structural:
        if not isinstance(predicate, Mapping) or not isinstance(
            predicate.get("path_template"), str
        ):
            return False, True
        operator = predicate.get("operator")
        if not isinstance(operator, str) or operator not in _PREDICATES:
            return False, True
        path = _wildcard_path(predicate["path_template"], actual_path).removeprefix("/config")
        found, value = _pointer_value(config, path)
        if not found:
            if operator == "not_false":
                continue
            return False, False
        if _contains_marker(value):
            return False, False
        if operator == "present" and value is None:
            return False, False
        if operator == "nonempty" and not (isinstance(value, (str, list, dict)) and len(value) > 0):
            return False, False
        if operator == "has_string_enum" and not _has_string_enum(value):
            return False, False
        if operator == "not_false" and value not in (None, True):
            return False, False
    return True, False


def _contains_marker(value: object) -> bool:
    pending = [value]
    visited = 0
    while pending and visited < _MAX_NODES:
        item = pending.pop()
        visited += 1
        if isinstance(item, Mapping):
            if "$param" in item:
                return True
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


def _has_string_enum(value: object) -> bool:
    return isinstance(value, Mapping) and any(
        isinstance(entry, Mapping)
        and isinstance(entry.get("enum"), list)
        and bool(entry["enum"])
        and all(isinstance(item, str) for item in entry["enum"])
        for entry in value.values()
    )


def _declaration_fits_slot(raw: object, slot: Mapping[str, object]) -> bool:
    if (
        not isinstance(raw, Mapping)
        or not isinstance(raw.get("type"), str)
        or raw.get("type") not in _CONTROL_TYPES
    ):
        return False
    kind = raw["type"]
    slot_types = slot.get("control_types")
    schema = slot.get("expected_value_schema")
    if (
        not isinstance(slot_types, Sequence)
        or isinstance(slot_types, (str, bytes))
        or kind not in slot_types
    ):
        return False
    if not isinstance(schema, Mapping):
        return False
    schema_types = schema.get("types")
    if (
        not isinstance(schema_types, Sequence)
        or isinstance(schema_types, (str, bytes))
        or kind not in schema_types
    ):
        return False
    default = raw.get("default")
    if kind == "number":
        if not _finite_number(default):
            return False
        if schema.get("integer") and not _safe_integer(default):
            return False
        minimum, maximum = schema.get("minimum"), schema.get("maximum")
        return (minimum is None or _finite_number(minimum) and default >= minimum) and (
            maximum is None or _finite_number(maximum) and default <= maximum
        )
    if kind == "boolean":
        return type(default) is bool
    if kind in ("string", "enum"):
        if (
            not isinstance(default, str)
            or not _unicode_ok(default)
            or len(default) > _MAX_STRING_SCALARS
        ):
            return False
        for source, field, bound_limit in (
            (raw, "min_length", _MAX_STRING_SCALARS),
            (raw, "max_length", _MAX_STRING_SCALARS),
            (schema, "min_length", _MAX_STRING_SCALARS),
            (schema, "max_length", 16_384),
        ):
            bound = source.get(field)
            if bound is not None and not _bounded_int(bound, bound_limit):
                return False
            if bound is not None and field == "min_length" and len(default) < bound:
                return False
            if bound is not None and field == "max_length" and len(default) > bound:
                return False
        choices = schema.get("allowed_values")
        if choices is not None and (
            not isinstance(choices, Sequence)
            or isinstance(choices, (str, bytes))
            or any(not isinstance(choice, str) for choice in choices)
            or default not in choices
        ):
            return False
        authored_choices = raw.get("allowed_values", [])
        return kind != "enum" or (
            isinstance(authored_choices, list) and default in authored_choices
        )
    if not isinstance(default, list) or len(default) > _MAX_LIST_ITEMS:
        return False
    if any(
        not isinstance(item, str) or not _unicode_ok(item) or len(item) > _MAX_STRING_SCALARS
        for item in default
    ):
        return False
    for field, bound_limit in (
        ("item_max_length", _MAX_STRING_SCALARS),
        ("min_items", _MAX_LIST_ITEMS),
        ("max_items", _MAX_LIST_ITEMS),
    ):
        bound = schema.get(field)
        if bound is not None and not _bounded_int(bound, bound_limit):
            return False
        if (
            field == "item_max_length"
            and bound is not None
            and any(len(item) > bound for item in default)
        ):
            return False
        if field == "min_items" and bound is not None and len(default) < bound:
            return False
        if field == "max_items" and bound is not None and len(default) > bound:
            return False
    allowed = raw.get("allowed_values")
    return allowed is None or (
        isinstance(allowed, list) and all(item in allowed for item in default)
    )


def _validate_declaration(key: object, raw: object, diagnostics: list[ControlDiagnostic]) -> None:
    pointer = f"/controls/{_escape(key)}" if isinstance(key, str) else "/controls"
    if not isinstance(key, str) or not _KEY.fullmatch(key) or key in _FORBIDDEN_KEYS:
        diagnostics.append(
            _diag("control_declaration_invalid", pointer, "Control key has an unsupported shape.")
        )
        return
    if not isinstance(raw, Mapping):
        diagnostics.append(
            _diag("control_declaration_invalid", pointer, "Control declaration must be an object.")
        )
        return
    kind = raw.get("type")
    if not isinstance(kind, str) or kind not in _CONTROL_TYPES:
        diagnostics.append(
            _diag("control_declaration_invalid", pointer + "/type", "Control type is unsupported.")
        )
        return
    if "default" not in raw:
        diagnostics.append(
            _diag("control_default_required", pointer + "/default", "A default value is required.")
        )
        return
    common = {"type", "label", "description", "default"}
    fields = {
        "number": {"minimum", "maximum", "integer"},
        "boolean": set(),
        "string": {"min_length", "max_length"},
        "enum": {"allowed_values"},
        "string_list": {"allowed_values", "min_items", "max_items"},
    }[kind]
    if set(raw) - common - fields:
        diagnostics.append(
            _diag(
                "control_declaration_invalid",
                pointer,
                "Declaration contains type-inapplicable fields.",
            )
        )
        return
    label = raw.get("label")
    if not isinstance(label, str) or not label or len(label) > 256 or not _unicode_ok(label):
        diagnostics.append(
            _diag(
                "control_declaration_invalid",
                pointer + "/label",
                "A bounded Unicode label is required.",
            )
        )
    description = raw.get("description")
    if description is not None and (
        not isinstance(description, str) or len(description) > 1024 or not _unicode_ok(description)
    ):
        diagnostics.append(
            _diag(
                "control_declaration_invalid",
                pointer + "/description",
                "Description must be bounded Unicode text.",
            )
        )
    error = _value_error(kind, raw.get("default"))
    if error:
        diagnostics.append(_diag("control_value_out_of_bounds", pointer + "/default", error))
    if kind == "number":
        minimum, maximum = raw.get("minimum"), raw.get("maximum")
        bounds_valid = _finite_number(minimum) and _finite_number(maximum)
        if not bounds_valid or cast(int | float, minimum) > cast(int | float, maximum):
            diagnostics.append(
                _diag(
                    "control_declaration_invalid",
                    pointer,
                    "Numeric controls require finite ordered bounds.",
                )
            )
        integer = raw.get("integer", False)
        if type(integer) is not bool:
            diagnostics.append(
                _diag(
                    "control_declaration_invalid",
                    pointer + "/integer",
                    "Integer must be a boolean.",
                )
            )
        if integer is True and any(
            not _safe_integer(raw.get(field)) for field in ("minimum", "maximum", "default")
        ):
            diagnostics.append(
                _diag(
                    "control_declaration_invalid",
                    pointer,
                    "Integer values and bounds must be safe integers.",
                )
            )
        default = raw.get("default")
        if (
            _finite_number(default)
            and _finite_number(minimum)
            and _finite_number(maximum)
            and not cast(int | float, minimum)
            <= cast(int | float, default)
            <= cast(int | float, maximum)
        ):
            diagnostics.append(
                _diag(
                    "control_value_out_of_bounds",
                    pointer + "/default",
                    "Default is outside its bounds.",
                )
            )
    if kind == "string":
        for field in ("min_length", "max_length"):
            if (
                field in raw
                and raw[field] is not None
                and not _bounded_int(raw[field], _MAX_STRING_SCALARS)
            ):
                diagnostics.append(
                    _diag(
                        "control_declaration_invalid",
                        pointer + "/" + field,
                        "String bounds must be bounded nonnegative integers.",
                    )
                )
        if (
            _bounded_int(raw.get("min_length"), _MAX_STRING_SCALARS)
            and _bounded_int(raw.get("max_length"), _MAX_STRING_SCALARS)
            and raw["min_length"] > raw["max_length"]
        ):
            diagnostics.append(
                _diag(
                    "control_declaration_invalid", pointer, "Minimum string length exceeds maximum."
                )
            )
        default = raw.get("default")
        if isinstance(default, str):
            if (
                _bounded_int(raw.get("min_length"), _MAX_STRING_SCALARS)
                and len(default) < raw["min_length"]
            ):
                diagnostics.append(
                    _diag(
                        "control_value_out_of_bounds",
                        pointer + "/default",
                        "Default is shorter than its minimum length.",
                    )
                )
            if (
                _bounded_int(raw.get("max_length"), _MAX_STRING_SCALARS)
                and len(default) > raw["max_length"]
            ):
                diagnostics.append(
                    _diag(
                        "control_value_out_of_bounds",
                        pointer + "/default",
                        "Default exceeds its maximum length.",
                    )
                )
    if kind in ("enum", "string_list"):
        choices = raw.get("allowed_values")
        if kind == "enum" or choices is not None:
            if (
                not isinstance(choices, list)
                or not choices
                or len(choices) > _MAX_LIST_ITEMS
                or any(
                    not isinstance(item, str)
                    or not item
                    or not _unicode_ok(item)
                    or len(item) > _MAX_STRING_SCALARS
                    for item in choices
                )
                or len(set(choices)) != len(choices)
            ):
                diagnostics.append(
                    _diag(
                        "control_declaration_invalid",
                        pointer + "/allowed_values",
                        "Allowed values must be bounded unique nonempty strings.",
                    )
                )
        if (
            kind == "enum"
            and isinstance(raw.get("default"), str)
            and isinstance(choices, list)
            and raw["default"] not in choices
        ):
            diagnostics.append(
                _diag(
                    "control_value_out_of_bounds",
                    pointer + "/default",
                    "Enum default must be one of its allowed values.",
                )
            )
    if kind == "string_list":
        for field in ("min_items", "max_items"):
            if (
                field in raw
                and raw[field] is not None
                and not _bounded_int(raw[field], _MAX_LIST_ITEMS)
            ):
                diagnostics.append(
                    _diag(
                        "control_declaration_invalid",
                        pointer + "/" + field,
                        "List bounds must be bounded nonnegative integers.",
                    )
                )
        if (
            _bounded_int(raw.get("min_items"), _MAX_LIST_ITEMS)
            and _bounded_int(raw.get("max_items"), _MAX_LIST_ITEMS)
            and raw["min_items"] > raw["max_items"]
        ):
            diagnostics.append(
                _diag("control_declaration_invalid", pointer, "Minimum list size exceeds maximum.")
            )
        default = raw.get("default")
        if isinstance(default, list):
            if (
                _bounded_int(raw.get("min_items"), _MAX_LIST_ITEMS)
                and len(default) < raw["min_items"]
            ):
                diagnostics.append(
                    _diag(
                        "control_value_out_of_bounds",
                        pointer + "/default",
                        "Default list is shorter than its minimum.",
                    )
                )
            if (
                _bounded_int(raw.get("max_items"), _MAX_LIST_ITEMS)
                and len(default) > raw["max_items"]
            ):
                diagnostics.append(
                    _diag(
                        "control_value_out_of_bounds",
                        pointer + "/default",
                        "Default list exceeds its maximum.",
                    )
                )
            if isinstance(choices, list) and any(item not in choices for item in default):
                diagnostics.append(
                    _diag(
                        "control_value_out_of_bounds",
                        pointer + "/default",
                        "Default list contains an item outside allowed values.",
                    )
                )


def _value_error(kind: object, value: object) -> str | None:
    if kind == "number":
        return None if _finite_number(value) else "Default must be a finite JSON number."
    if kind == "boolean":
        return None if type(value) is bool else "Default must be a JSON boolean."
    if kind in ("string", "enum"):
        return (
            None
            if isinstance(value, str) and _unicode_ok(value) and len(value) <= _MAX_STRING_SCALARS
            else "Default must be a bounded Unicode string."
        )
    if not isinstance(value, list) or len(value) > _MAX_LIST_ITEMS:
        return "Default must be a bounded list of strings."
    return (
        None
        if all(
            isinstance(item, str) and _unicode_ok(item) and len(item) <= _MAX_STRING_SCALARS
            for item in value
        )
        else "Default must be a bounded list of strings."
    )


def _finite_number(value: object) -> bool:
    return (type(value) is int and abs(value) <= int(sys.float_info.max)) or (
        type(value) is float and math.isfinite(value)
    )


def _safe_integer(value: object) -> bool:
    return (
        _finite_number(value)
        and (type(value) is int or type(value) is float and value.is_integer())
        and abs(value) <= 9_007_199_254_740_991
    )


def _bounded_int(value: object, maximum: int) -> bool:
    return type(value) is int and 0 <= value <= maximum


def _walk(document: object) -> list[object]:
    pending = [document]
    values: list[object] = []
    while pending and len(values) <= _MAX_NODES:
        value = pending.pop()
        values.append(value)
        if isinstance(value, Mapping):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return values


def _resolve_defaults(
    document: Mapping[str, object],
    declarations: Mapping[str, object],
    references: Sequence[_ReferenceSite],
) -> object:
    copied = _copy_json(document)
    if not isinstance(copied, dict):
        return copied
    for reference in references:
        declaration = declarations.get(reference.key)
        if isinstance(declaration, Mapping) and "default" in declaration:
            _set_pointer(copied, reference.pointer, declaration["default"])
    copied.pop("controls", None)
    return copied


def _set_pointer(root: dict[str, object], pointer: str, replacement: object) -> None:
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]]
    if not parts:
        return
    node: object = root
    for part in parts[:-1]:
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return
    last = parts[-1]
    if isinstance(node, dict) and last in node:
        node[last] = replacement
    elif isinstance(node, list) and last.isdigit() and int(last) < len(node):
        node[int(last)] = replacement
