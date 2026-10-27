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
    "POLICY_RULE_INCOMPATIBLE",
    "RULE_BINDING_INCOMPATIBLE",
)


@pytest.mark.parametrize("name", _RULE_CODES)
def test_native_enum_members_and_public_codes(name: str) -> None:
    assert ErrorCode[name].value == name
    assert make_public_error_code(ErrorCode[name]) == f"NORMA_{name}"
    with pytest.raises(ValueError):
        ErrorCode("UNKNOWN_RULE_CODE")


@pytest.mark.parametrize(
    "retired", ["RULE_TEMPLATE_CONFLICT", "TEMPLATE_PARAMETER_INVALID", "TEMPLATE_RENDER_ERROR"]
)
def test_retired_template_diagnostics_are_not_public_codes(retired: str) -> None:
    assert retired not in ErrorCode.__members__
    response = httpx.Response(
        409,
        json={"error_code": retired},
        request=httpx.Request("GET", "https://tenant.example/api/v1/rules"),
    )
    assert NormaAPIError(response).code == "NORMA_CONFLICT"


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


def test_registration_step_conflict_explains_manual_import_without_losing_diagnostics() -> None:
    # Given: strict registration rejected a changed checkpoint schema.
    payload = {
        "error_code": "SCHEMA_INCOMPATIBLE",
        "detail": "Step schema conflict for (type='tool', name='publish').",
        "hint": "Please use a different step name or ensure schemas match exactly.",
        "errors": [
            {
                "resource": "Step",
                "field": "schema",
                "code": "schema_mismatch",
                "message": "Existing schema differs from incoming schema for step 'publish'",
            }
        ],
        "details": {"kind": "Step", "name": "publish"},
    }
    request = httpx.Request("POST", "https://tenant.example/api/v1/agents/register")
    response = httpx.Response(409, json=payload, request=request)

    # When: callers read the public SDK exception.
    error = NormaAPIError(response)

    # Then: rejection remains intact and the recovery names the actual Console flow.
    assert error.status_code == 409
    assert error.code == "NORMA_SCHEMA_INCOMPATIBLE"
    assert error.type == "urn:norma:error:schema-incompatible"
    assert error.request is request
    assert error.response.json() == payload
    assert "rejected proposal was not saved or imported automatically" in error.hint
    assert "complete SDK steps array as JSON" in error.hint
    assert "norma.checkpoint_schemas()" in error.hint
    assert (
        "Console → Agents → your agent → Connection → Edit step schemas → Import schemas"
        in error.hint
    )
    assert "paste" in error.hint
    assert "Inspect the diff" in error.hint
    assert "Save changes" in error.hint
    assert "conflict_mode='strict'" in error.hint
    assert "different step name" not in error.hint
    assert "overwrite" not in error.hint
    assert "force_replace" not in error.hint


def test_control_diagnostics_preserve_only_known_codes_and_bounded_source_pointers() -> None:
    request = httpx.Request("PUT", "https://tenant.example/api/v1/rules/7/data")
    response = httpx.Response(422, json={
        "error_code": "VALIDATION_ERROR",
        "errors": [
            {
                "field": "/controls/age/default",
                "code": "control_value_out_of_bounds",
                "message": "raw value 999",
            },
            {"field": "/controls/age/~2bad", "code": "control_reference_invalid"},
            {"field": "/controls/age/default", "code": "unknown_private_code"},
            {
                "field": "/controls/age/default",
                "code": "control_reference_invalid",
                "message": "synthetic-secret",
            },
            {
                "field": "/values",
                "code": "control_evidence_size_exceeded",
                "message": "private capacity details",
            },
        ],
    }, request=request)

    error = NormaAPIError(response)

    assert [(item.code, item.source_pointer) for item in error.diagnostics] == [
        ("control_value_out_of_bounds", "/controls/age/default"),
        ("control_reference_invalid", "/controls/age/default"),
        ("control_evidence_size_exceeded", "/values"),
    ]
    assert "synthetic-secret" not in str(error)
    assert "private capacity details" not in str(error)


@pytest.mark.parametrize(
    ("name", "status", "form"),
    [
        (name, status, form)
        for name, status in (
            ("CONTROL_CONTRACT_REQUIRED", 400),
            ("CONTROL_CONTRACT_DISABLED", 400),
            ("CONTROL_REVISION_CONFLICT", 409),
            ("CONTROL_STATE_UNAVAILABLE", 503),
        )
        for form in ("native", "public", "urn")
    ],
)
def test_control_lifecycle_errors_keep_stable_code_and_sanitized_message(
    name: str, status: int, form: str
) -> None:
    supplied = name
    if form == "public":
        supplied = "NORMA_" + name
    elif form == "urn":
        supplied = "urn:norma:error:" + name.lower().replace("_", "-")
    request = httpx.Request("PUT", "https://tenant.example/api/v1/agents/a/rule-controls")
    response = httpx.Response(
        status,
        json={"error_code": supplied, "detail": "private control values must never appear"},
        request=request,
    )

    error = NormaAPIError(response)

    assert ErrorCode[name].value == name
    assert error.code == f"NORMA_{name}"
    assert error.status_code == status
    assert name.lower().replace("_", "-") in error.type
    assert "private control values" not in str(error)


def test_control_code_on_forbidden_response_keeps_generic_safe_denial() -> None:
    request = httpx.Request("PUT", "https://tenant.example/api/v1/agents/a/rule-controls")
    response = httpx.Response(
        403,
        json={"error_code": "CONTROL_STATE_UNAVAILABLE", "detail": "private override payload"},
        request=request,
    )

    error = NormaAPIError(response)

    assert error.code == "NORMA_ACCESS_DENIED"
    assert "private override payload" not in str(error)


def test_stale_registration_review_keeps_reload_recovery() -> None:
    payload = {
        "error_code": "SCHEMA_INCOMPATIBLE",
        "detail": "This agent's registration changed after you opened the schema review.",
        "hint": (
            "Reload the saved registration in Console, review your proposed schemas again, "
            "then confirm the update."
        ),
        "details": {"kind": "Agent"},
    }
    request = httpx.Request("POST", "https://tenant.example/api/v1/agents/register")
    response = httpx.Response(409, json=payload, request=request)
    error = NormaAPIError(response)
    assert error.code == "NORMA_SCHEMA_INCOMPATIBLE"
    assert error.status_code == 409
    assert "Reload the saved registration" in error.hint
    assert "Import schemas" not in error.hint
    assert error.response.json() == payload


@pytest.mark.parametrize(
    ("method", "path", "status", "code", "kind"),
    [
        ("POST", "/api/v1/agents/register", 409, "SCHEMA_INCOMPATIBLE", "Evaluator"),
        ("POST", "/api/v1/checks", 409, "SCHEMA_INCOMPATIBLE", "Step"),
        ("GET", "/api/v1/agents/register", 409, "SCHEMA_INCOMPATIBLE", "Step"),
        ("POST", "/api/v1/agents/register", 422, "SCHEMA_INCOMPATIBLE", "Step"),
        ("POST", "/api/v1/agents/register", 409, "AGENT_NAME_CONFLICT", "Step"),
    ],
)
def test_schema_import_guidance_is_limited_to_registration_step_conflicts(
    method: str, path: str, status: int, code: str, kind: str
) -> None:
    request = httpx.Request(method, "https://tenant.example" + path)
    response = httpx.Response(
        status, json={"error_code": code, "details": {"kind": kind}}, request=request
    )
    error = NormaAPIError(response)
    assert error.code == "NORMA_" + code
    assert error.status_code == status
    assert "Import schemas" not in error.hint
    assert "checkpoint_schemas()" not in error.hint


@pytest.mark.parametrize(
    "diagnostic",
    [
        {"details": {"kind": "Step"}},
        {"errors": [{"resource": "Step", "code": "schema_mismatch"}]},
        {"detail": "Step schema conflict for (type='tool', name='publish')."},
    ],
)
def test_step_schema_recovery_supports_direct_and_hosted_runtime_diagnostics(
    diagnostic: dict[str, object],
) -> None:
    request = httpx.Request("POST", "https://tenant.example/api/runtime/api/v1/agents/register")
    response = httpx.Response(
        409, json={"code": "NORMA_SCHEMA_INCOMPATIBLE", **diagnostic}, request=request
    )
    error = NormaAPIError(response)
    assert "Import schemas" in error.hint


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
