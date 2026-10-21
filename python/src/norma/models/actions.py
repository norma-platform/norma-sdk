# Modified for Norma.
"""Shared Rule-action types and normalization helpers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, cast

type ActionDecision = Literal["deny", "steer", "observe"]

_CANONICAL_ACTIONS = frozenset({"deny", "steer", "observe"})


def validate_action(action: str) -> ActionDecision:
    """Validate that *action* is one of the canonical action values.

    Use this on public API boundaries (Rule create/update, query filters)
    where unrecognized values must be rejected.
    """
    if action in _CANONICAL_ACTIONS:
        return cast(ActionDecision, action)
    raise ValueError(f"Invalid action {action!r}. Must be one of: deny, steer, observe.")


def validate_action_list(actions: Sequence[str]) -> list[ActionDecision]:
    """Validate a list of actions, preserving order and removing duplicates."""
    validated: list[ActionDecision] = []
    seen: set[ActionDecision] = set()
    for action in actions:
        canonical = validate_action(action)
        if canonical in seen:
            continue
        seen.add(canonical)
        validated.append(canonical)
    return validated


def normalize_action(action: str) -> ActionDecision:
    """Validate a native Rule action without accepting alternate spellings."""
    return validate_action(action)


def normalize_action_list(actions: Sequence[str]) -> list[ActionDecision]:
    """Normalize a list of actions while preserving order and removing duplicates."""
    normalized: list[ActionDecision] = []
    seen: set[ActionDecision] = set()
    for action in actions:
        canonical = normalize_action(action)
        if canonical in seen:
            continue
        seen.add(canonical)
        normalized.append(canonical)
    return normalized


def expand_action_filter(actions: Sequence[ActionDecision]) -> list[str]:
    """Validate and deduplicate native action filters."""
    return list(validate_action_list(actions))
