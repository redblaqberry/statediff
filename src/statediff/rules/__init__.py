"""Rule dispatch: the seven scenario-facing rules."""

from __future__ import annotations

from typing import Callable

from .base import RuleContext, RuleOutcome
from . import (
    compensated,
    correlated,
    count_delta,
    event_exists,
    idempotent,
    transition,
    unchanged,
)

EVALUATORS: dict[str, Callable[[object, RuleContext], RuleOutcome]] = {
    "transition": transition.evaluate,
    "count_delta": count_delta.evaluate,
    "unchanged": unchanged.evaluate,
    "event_exists": event_exists.evaluate,
    "idempotent": idempotent.evaluate,
    "compensated": compensated.evaluate,
    "correlated": correlated.evaluate,
}

__all__ = ["EVALUATORS", "RuleContext", "RuleOutcome"]
