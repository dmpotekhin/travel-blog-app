"""Narrative layer (Phase 3): SlidePlan JSON, deterministic and sourced.

The planner is the only place that decides what appears on a slide; the renderer
just paints it. Everything it emits traces back to a fact, quote, code snippet or
metric the resolver read.
"""

from .planner import SlideDraft, SlidePlanner, clip

__all__ = ["SlideDraft", "SlidePlanner", "clip"]
