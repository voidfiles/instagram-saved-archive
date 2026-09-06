"""Typed, sanitized boundary around Instagram access."""

from .client import InstaloaderClient
from .protocol import InstagramClient

__all__ = ["InstagramClient", "InstaloaderClient"]
