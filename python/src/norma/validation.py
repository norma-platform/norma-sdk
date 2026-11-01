# Modified for Norma.
"""Validation helpers for SDK inputs."""

from __future__ import annotations

from _norma_config.identity import ensure_agent_name as ensure_agent_name


def ensure_step_type(value: str, field_name: str = "step_type") -> str:
    """Return a normalized non-empty step type or raise ``ValueError``."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()
