# Modified for Norma.
"""Safe public HTTP errors for every Norma SDK operation."""

import re
from dataclasses import dataclass
from email.utils import format_datetime, parsedate_to_datetime
from typing import Any

import httpx

from norma._wire.errors import decode_error_code as _decode_error_code
from norma.models.errors import ErrorCode, make_public_error_code

_AUTH_CODES = frozenset({"AUTH_MISSING_KEY", "AUTH_INVALID_KEY", "AUTH_UPSTREAM_REJECTED"})
_SERVICE_CODES = frozenset(
    {
        "DATABASE_ERROR",
        "INTERNAL_ERROR",
        "CHECK_FAILED",
        "AUTH_MISCONFIGURED",
        "AUTH_UPSTREAM_REJECTED",
        "CONTROL_STATE_UNAVAILABLE",
    }
)
_DIAGNOSTIC_CODES = frozenset(
    str(code)
    for code in ErrorCode
    if not str(code).startswith("AUTH_")
    and code not in _SERVICE_CODES
    and code != ErrorCode.METHOD_NOT_ALLOWED
)
_REQUEST_ID = re.compile(
    r"(?:[a-f0-9]{32}|[a-f0-9]{64}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})\Z", re.I
)
_HTTP_DATE = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{2} "
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{4} "
    r"\d{2}:\d{2}:\d{2} GMT\Z"
)
_CONTROL_DIAGNOSTIC_CODES = frozenset({
    "control_default_required", "control_declaration_invalid", "control_reference_invalid",
    "control_reference_not_allowed", "control_reference_unknown", "control_unused",
    "control_symbol_conflict", "control_type_mismatch", "control_value_out_of_bounds",
    "control_destination_invalid", "control_execution_unsupported",
    "control_capability_unavailable", "control_override_incompatible",
    "control_evidence_size_exceeded",
})
_JSON_POINTER = re.compile(r"(?:/(?:[^~/]|~[01])*)*\Z")


@dataclass(frozen=True, slots=True)
class ControlRequestDiagnostic:
    """A sanitized compiler diagnostic without echoed values or messages."""

    code: str
    source_pointer: str


def _control_diagnostics(payload: dict[str, Any]) -> tuple[ControlRequestDiagnostic, ...]:
    raw = payload.get("errors")
    if not isinstance(raw, list):
        return ()
    result: list[ControlRequestDiagnostic] = []
    for item in raw[:50]:
        if not isinstance(item, dict):
            continue
        code, pointer = item.get("code"), item.get("field")
        if (
            isinstance(code, str)
            and code in _CONTROL_DIAGNOSTIC_CODES
            and isinstance(pointer, str)
            and len(pointer) <= 1024
            and _JSON_POINTER.fullmatch(pointer)
        ):
            result.append(ControlRequestDiagnostic(code=code, source_pointer=pointer))
    return tuple(result)


def _known_code(payload: dict[str, Any], status: int) -> str | None:
    allowed = (
        _AUTH_CODES
        if status == 401
        else _SERVICE_CODES
        if status >= 500
        else _DIAGNOSTIC_CODES
        if status in {400, 404, 409, 422}
        else frozenset()
    )
    for field in ("code", "type", "error_code"):
        candidate = payload.get(field)
        if not isinstance(candidate, str):
            continue
        code = _decode_error_code(candidate)
        if code in allowed:
            return make_public_error_code(code)
    return None


def _retry_after(value: str | None) -> str | None:
    if value is None:
        return None
    if re.fullmatch(r"[0-9]{1,10}", value) and int(value) <= 2_147_483_647:
        return str(int(value))
    if _HTTP_DATE.fullmatch(value):
        try:
            parsed = parsedate_to_datetime(value)
            if format_datetime(parsed, usegmt=True) == value:
                return value
        except (ValueError, OverflowError):
            pass
    return None


def _step_schema_conflict(payload: dict[str, Any]) -> bool:
    details = payload.get("details")
    if isinstance(details, dict) and details.get("kind") == "Step":
        return True
    errors = payload.get("errors")
    if isinstance(errors, list) and any(
        isinstance(error, dict)
        and error.get("resource") == "Step"
        and error.get("code") == "schema_mismatch"
        for error in errors
    ):
        return True
    detail = payload.get("detail")
    return isinstance(detail, str) and detail.startswith("Step schema conflict for ")


class NormaAPIError(httpx.HTTPStatusError):
    """A rejected API request with stable, safe diagnostics.

    ``request`` and ``response`` remain available for existing httpx handlers.
    Their raw content may contain private data; log the normalized fields instead.
    """

    def __init__(self, response: httpx.Response) -> None:
        payload: dict[str, Any] = {}
        try:
            body = response.json()
            if isinstance(body, dict):
                payload = body
        except (ValueError, httpx.ResponseNotRead):
            pass
        status = response.status_code
        supplied = _known_code(payload, status)
        if status == 401:
            code = supplied or "NORMA_AUTHENTICATION_REQUIRED"
            detail = "Norma could not authenticate this request."
            hint = (
                "Check NORMA_AGENT_KEY and NORMA_RUNTIME_URL against the agent's "
                "Connection page in Console."
            )
        elif status == 403:
            code = "NORMA_ACCESS_DENIED"
            detail = "This key does not allow the requested action."
            hint = (
                "Ask a workspace owner to confirm the key's allowed agent "
                "and requested action in Console."
            )
            if response.request.url.path.endswith("/agents/register"):
                hint = (
                    "In Console, create or select the agent and confirm "
                    "the key allows its exact name. "
                    "Use conflict_mode='strict' for an existing allowed agent; Agent Keys cannot "
                    "create identities or use overwrite mode. "
                    "Ask a workspace owner to make setup changes. "
                    "Do not continue the agent after this failure."
                )
        elif status >= 500:
            code = supplied or "NORMA_RUNTIME_UNAVAILABLE"
            detail = "Norma could not complete this request."
            hint = (
                "Retry later. If the problem continues, share the request ID "
                "with your workspace owner."
            )
        elif status == 429:
            code = "NORMA_RATE_LIMITED"
            detail = "Norma's request limit was reached."
            hint = "Wait before retrying and honor Retry-After when provided."
        elif status == 409:
            code = str(supplied or "NORMA_CONFLICT")
            detail = "The request conflicts with the saved configuration."
            hint = "Review the saved configuration before retrying."
            if (
                code == "NORMA_SCHEMA_INCOMPATIBLE"
                and response.request.method == "POST"
                and response.request.url.path.endswith("/api/v1/agents/register")
            ):
                if payload.get("detail") == (
                    "This agent's registration changed after you opened the schema review."
                ):
                    hint = (
                        "Reload the saved registration in Console, inspect your proposed schemas "
                        "against the current registration, then choose Save changes."
                    )
                elif _step_schema_conflict(payload):
                    hint = (
                        "The rejected proposal was not saved or imported automatically. "
                        "Export the complete SDK steps array as JSON "
                        "(norma.checkpoint_schemas() for Python decorators). Ask a workspace owner "
                        "to open Console → Agents → your agent → Connection → Edit step schemas → "
                        "Import schemas (or paste the JSON). Inspect the diff, choose "
                        "Save changes, then retry initialization with conflict_mode='strict'."
                    )
        elif status == 404:
            code = str(supplied or "NORMA_NOT_FOUND")
            detail = "The requested Norma resource was not found."
            hint = "Check the resource name and NORMA_RUNTIME_URL in Console."
        elif status in {400, 422}:
            code = supplied or "NORMA_VALIDATION_ERROR"
            detail = "Norma could not validate this request."
            hint = "Check the request fields and their expected types."
        elif status == 405:
            code = "NORMA_METHOD_NOT_ALLOWED"
            detail = "This HTTP method is not available for the Norma endpoint."
            hint = "Check the endpoint and HTTP method in your request."
        else:
            code = "NORMA_REQUEST_FAILED"
            detail = "Norma rejected this request."
            hint = "Check the request against the Norma SDK contract and saved agent configuration."
        metadata = payload.get("metadata")
        self.request_id: str | None = None
        candidates = (
            response.headers.get("x-request-id"),
            payload.get("request_id"),
            metadata.get("request_id") if isinstance(metadata, dict) else None,
        )
        for request_id in candidates:
            if isinstance(request_id, str) and _REQUEST_ID.fullmatch(request_id):
                self.request_id = request_id
                break
        self.retry_after = _retry_after(response.headers.get("retry-after"))
        self.diagnostics = _control_diagnostics(payload)
        self.code = code
        self.hint = hint
        self.status = self.status_code = status
        self.type = "urn:norma:error:" + code.removeprefix("NORMA_").lower().replace("_", "-")
        super().__init__(
            f"{code} (HTTP {status}): {detail} {hint}",
            request=response.request,
            response=response,
        )


def raise_for_norma_status(response: httpx.Response) -> None:
    """Preserve httpx status handling while normalizing rejected API requests."""
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.is_error:
            raise NormaAPIError(exc.response) from None
        raise
