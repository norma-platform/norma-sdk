# Modified for Norma.
"""Optional framework adapter imports."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .plugin import NormaPlugin
    from .steering import NormaSteeringHandler

__all__ = ["NormaPlugin", "NormaSteeringHandler"]


def __getattr__(name: str) -> type:
    if name == "NormaPlugin":
        from .plugin import NormaPlugin

        return NormaPlugin
    if name == "NormaSteeringHandler":
        from .steering import NormaSteeringHandler

        return NormaSteeringHandler
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
