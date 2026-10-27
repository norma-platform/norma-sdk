"""Public Console control helper fixtures shared with the TypeScript SDK."""

import json
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from norma.controls import ControlDeclaration, validate_authored_controls

FIXTURE = Path(__file__).parents[2] / "tests" / "fixtures" / "controls" / "control-cases.json"

_MINIMAL_DECLARATIONS: tuple[ControlDeclaration, ...] = (
    {"type": "number", "label": "N", "default": 1, "minimum": 0, "maximum": 2},
    {"type": "boolean", "label": "B", "default": False},
    {"type": "string", "label": "S", "default": ""},
    {"type": "enum", "label": "E", "default": "x", "allowed_values": ["x"]},
    {"type": "string_list", "label": "L", "default": []},
)


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_shared_control_fixtures_match_the_independent_oracle() -> None:
    fixture = _fixture()
    for case in fixture["cases"]:
        source = case["source"]
        if case.get("source_as_mapping"):
            source = json.loads(json.dumps(source))
        capabilities = (
            None
            if case.get("without_capabilities")
            else case.get("capabilities", fixture["capabilities"])
        )

        result = validate_authored_controls(source, capabilities)

        assert result.status == case["status"], case["name"]
        assert result.source_validation == case["source_validation"], case["name"]
        assert result.requires_server_validation is (
            case.get("requires_server_validation", False)
            or case["status"] == "requires_server_validation"
        ), case["name"]
        expected_code = case.get("diagnostic_code")
        if expected_code:
            assert expected_code in {diagnostic.code for diagnostic in result.diagnostics}, case[
                "name"
            ]
        expected_reference = case.get("reference_projection")
        if expected_reference:
            assert len(result.references) == 1, case["name"]
            reference = result.references[0]
            assert reference.source_pointer == expected_reference["source_pointer"]
            assert reference.destination_pointer == expected_reference["destination_pointer"]
            assert reference.evaluator_pointer == expected_reference["evaluator_pointer"]


def test_control_helper_returns_immutable_copies() -> None:
    fixture = _fixture()
    case = fixture["cases"][0]
    result = validate_authored_controls(case["source"], fixture["capabilities"])

    assert result.authored_document is not None
    try:
        result.authored_document["description"] = "mutated"  # type: ignore[index]
    except TypeError:
        pass
    else:
        raise AssertionError("the admitted document must be read-only")


def test_raw_helper_preserves_proto_key_and_typed_reference_projection() -> None:
    fixture = _fixture()
    example = fixture["cases"][0]
    result = validate_authored_controls(example["source"], fixture["capabilities"])

    assert len(result.references) == 1
    assert result.references[0].key == "limit"
    assert result.references[0].source_pointer.endswith("/min")
    document = validate_authored_controls('{"__proto__":{"safe":true}}', None)
    assert document.authored_document is not None
    prototype_key = document.authored_document["__proto__"]
    assert isinstance(prototype_key, Mapping)
    assert prototype_key["safe"] is True


def test_parsed_source_rejects_non_builtin_mapping_implementations() -> None:
    result = validate_authored_controls(MappingProxyType({"execution": "server"}), None)
    assert result.status == "invalid"
    assert result.source_validation == "invalid"


def test_control_validator_rejects_retired_root_bindings_but_keeps_evaluator_json() -> None:
    base = {"execution": "server", "condition": {}, "action": {"decision": "deny"}}
    for field in ("template", "template_values", "parameters"):
        result = validate_authored_controls({**base, field: {}}, None)
        assert result.status == "invalid"
        assert result.diagnostics[0].source_pointer == f"/{field}"
    nested = {**base, "condition": {"evaluator": {"name": "json", "config": {
        "parameters": {"customer": {"threshold": 4}},
    }}}}
    nested_result = validate_authored_controls(nested, None)
    assert nested_result.status != "invalid"
    assert nested_result.authored_document["condition"]["evaluator"]["config"]["parameters"] == {
        "customer": {"threshold": 4}
    }
