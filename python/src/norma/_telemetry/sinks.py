# Modified for Norma.
"""Shared sink contracts for Rule execution event delivery."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from norma.models import RuleCheckEvent


@dataclass(frozen=True)
class SinkResult:
    """Summarize acknowledgment independently from new storage.

    ``accepted`` includes known idempotent replays. ``stored`` is optional because
    queued/custom sinks may not know final persistence. Replays are never drops.
    """

    accepted: int
    dropped: int = 0
    duplicates: int = 0
    stored: int | None = None

    @property
    def success(self) -> bool:
        """Return True when at least one event was accepted."""
        return self.accepted > 0


class RuleEventSink(Protocol):
    """Write-side abstraction for delivering Rule execution events."""

    def write_events(self, events: Sequence[RuleCheckEvent]) -> SinkResult:
        """Write a batch of Rule execution events."""


class BaseRuleEventSink(RuleEventSink):
    """Minimal helper base for sink implementations."""

    def is_active(self) -> bool:
        """Return whether the sink is currently able to deliver events."""
        return True

    def write_event(self, event: RuleCheckEvent) -> SinkResult:
        """Write a single Rule execution event."""
        return self.write_events([event])

    def write_events(self, events: Sequence[RuleCheckEvent]) -> SinkResult:
        """Write a batch of Rule execution events."""
        accepted = 0
        dropped = 0
        for event in events:
            result = self.write_event(event)
            accepted += result.accepted
            dropped += result.dropped
        return SinkResult(accepted=accepted, dropped=dropped)


class AsyncRuleEventSink(Protocol):
    """Async write-side abstraction for delivering Rule execution events."""

    async def write_events(self, events: Sequence[RuleCheckEvent]) -> SinkResult:
        """Write a batch of Rule execution events."""


class BaseAsyncRuleEventSink(AsyncRuleEventSink):
    """Minimal async helper base for sink implementations."""

    async def write_event(self, event: RuleCheckEvent) -> SinkResult:
        """Write a single Rule execution event."""
        return await self.write_events([event])

    async def write_events(self, events: Sequence[RuleCheckEvent]) -> SinkResult:
        """Write a batch of Rule execution events."""
        accepted = 0
        dropped = 0
        for event in events:
            result = await self.write_event(event)
            accepted += result.accepted
            dropped += result.dropped
        return SinkResult(accepted=accepted, dropped=dropped)
