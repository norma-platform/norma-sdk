# Modified for Norma.
"""Public enums and HTTP exceptions share Rule-oriented error identifiers."""

import importlib

import httpx
import pytest

import norma
from norma.errors import NormaAPIError
from norma.models.errors import ErrorCode, make_public_error_code

_RULE_CODES = (
    "RULE_NOT_FOUND",
    "RULE_VERSION_NOT_FOUND",
    "RULE_BINDING_NOT_FOUND",
    "RULE_NAME_CONFLICT",
    "RULE_BINDING_CONFLICT",
    "RULE_IN_USE",
    "RULE_TEMPLATE_CONFLICT",
    "POLICY_RULE_INCOMPATIBLE",
    "RULE_BINDING_INCOMPATIBLE",
)


@pytest.mark.parametrize("name", _RULE_CODES)
def test_native_enum_members_and_public_codes(name: str) -> None:
    assert ErrorCode[name].value == name
    assert make_public_error_code(ErrorCode[name]) == f"NORMA_{name}"
    with pytest.raises(ValueError):
        ErrorCode("UNKNOWN_RULE_CODE")


@pytest.mark.parametrize("name", _RULE_CODES)
@pytest.mark.parametrize("form", ["native", "public", "urn"])
def test_http_errors_preserve_native_identifiers(name: str, form: str) -> None:
    code = "NORMA_" + name if form == "public" else name
    if form == "urn":
        code = "urn:norma:error:" + name.lower().replace("_", "-")
    request = httpx.Request("GET", "https://tenant.example/api/v1/rules")
    response = httpx.Response(409, json={"error_code": code}, request=request)
    error = NormaAPIError(response)
    assert error.code == make_public_error_code(ErrorCode[name])
    assert error.type == "urn:norma:error:" + name.lower().replace("_", "-")
    assert error.response is response


def test_public_modules_do_not_export_wire_helpers_or_session_state() -> None:
    for module_name in (
        "norma",
        "norma.checks",
        "norma.agents",
        "norma.operations",
        "norma.checkpoints",
        "norma.finish",
        "norma.errors",
    ):
        module = importlib.import_module(module_name)
        for name in (
            "state",
            "decision_from_wire",
            "normalize_answers",
            "rule_definition_from_wire",
            "feedback_from_wire",
            "decode_error_code",
            "request_check",
            "request_remote_check",
        ):
            assert not hasattr(module, name), (module_name, name)
    assert not hasattr(norma, "_raw_runtime_client")
