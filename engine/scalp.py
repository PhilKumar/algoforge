"""Compatibility wrapper for the canonical PhilForge scalp engine.

The application imports :mod:`scalp` directly. This module is kept only for
older imports that still reference ``engine.scalp``.
"""

from scalp import ScalpEngine, ScalpTrade

__all__ = ["ScalpEngine", "ScalpTrade"]
