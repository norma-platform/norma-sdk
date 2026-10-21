# Modified for Norma.
"""Decode native Norma error identifiers."""

from ..models.errors import ErrorCode


def decode_error_code(value: str) -> ErrorCode | None:
    """Accept native identifiers and their public URN representation."""
    if value.startswith("urn:norma:error:"):
        value = value.removeprefix("urn:norma:error:").upper().replace("-", "_")
    value = value.removeprefix("NORMA_")
    try:
        return ErrorCode(value)
    except ValueError:
        return None
