# Modified for Norma.
"""Optional framework adapter imports."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .plugin import NormaPlugin

__all__ = ["NormaPlugin"]


def __getattr__(name: str) -> type:
    if name == "NormaPlugin":
        from .plugin import NormaPlugin

        return NormaPlugin
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
