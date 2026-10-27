# Modified for Norma.
"""
Global state management for Norma SDK.

This module holds global state in a container object to avoid circular imports
between __init__.py and other modules. Both modules can import and modify
the same state object.
"""

from typing import TYPE_CHECKING, Any

from .control_contract import ControlContract

if TYPE_CHECKING:
    from norma.models import Agent

    from .client import NormaClient


class _StateContainer:
    """Container for global SDK state."""

    def __init__(self) -> None:
        self.current_agent: Agent | None = None
        self.initialization_failed = False
        self.rule_engine: Any = None
        self.client: NormaClient | None = None
        self.server_rules: list[dict[str, Any]] | None = None
        self.rule_cache_healthy = False
        self.server_url: str | None = None
        self.api_key: str | None = None
        self.api_key_header: str | None = None
        self.control_contract: ControlContract | None = None
        # Optional target context fixed at init() time; both fields are set
        # together or both remain None.
        self.target_type: str | None = None
        self.target_id: str | None = None

    def require_rule_cache(self) -> list[dict[str, Any]]:
        """Reject unknown coverage after initialization or a failed refresh."""
        if self.server_rules is None or not self.rule_cache_healthy:
            raise RuntimeError(
                "Norma Rule cache is unavailable; execution blocked until "
                "Rules refresh successfully."
            )
        return self.server_rules


# Singleton state instance
state = _StateContainer()
