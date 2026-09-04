"""Async-first library for running hercules-ci style effects.

The nixbot daemon imports this package directly. The `nixbot-effects`
CLI in `cli.py` is a thin standalone wrapper.
"""

from .errors import EffectError
from .eval import (
    check_effect,
    check_event_effect,
    check_scheduled_effect,
    list_all_event_effects,
    list_effects,
    list_event_effects,
    list_scheduled_effects,
)
from .graph import EffectMeta, EventEffectMeta
from .options import EffectsOptions
from .run import run_effect, run_event_effect, run_scheduled_effect

__all__ = [
    "EffectError",
    "EffectMeta",
    "EffectsOptions",
    "EventEffectMeta",
    "check_effect",
    "check_event_effect",
    "check_scheduled_effect",
    "list_all_event_effects",
    "list_effects",
    "list_event_effects",
    "list_scheduled_effects",
    "run_effect",
    "run_event_effect",
    "run_scheduled_effect",
]
