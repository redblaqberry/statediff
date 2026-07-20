"""event_exists: bounded count of matching events in the appended suffix.
Filters cover the event's top-level columns (type, actor, system, entity)
because acting identity is part of the audit contract, plus payload pins.
Covers the events it matched when its bounds are satisfied."""

from __future__ import annotations

from ..diff import event_atom_id
from ..scenario import (
    EventExistsSpec,
    count_describe,
    count_satisfied,
    require_count_spec,
    require_event_filters,
    require_payload_values,
)
from .base import RuleContext, RuleOutcome, event_evidence, event_matches, require_event_type


def evaluate(spec: EventExistsSpec, ctx: RuleContext) -> RuleOutcome:
    require_event_type(spec.id, spec.type)
    # The model validators again, for a spec built or mutated in Python. Nothing
    # below can stand in for them: `event_matches` answers "this event does not
    # carry that value", which reads identically whether the pin missed or
    # could never match, so a NaN pin (unequal to everything, itself included)
    # counted zero events and reported a forbidden effect as passing while
    # being incapable of firing.
    require_payload_values("payload_includes", spec.payload_includes)
    require_event_filters(spec)
    # The bound is the same story one step later. `count_satisfied` compares the
    # matches it counted against whatever this spec now holds and says only that
    # the two disagree, so `count: -1` counted the events honestly and then
    # reported a FORBIDDEN effect as passing, since no number of events could
    # ever equal a negative one.
    require_count_spec("count", spec.count)
    matching = [
        event for event in ctx.diff.suffix_events
        if event_matches(
            event, type_=spec.type, entity_type=spec.entity_type, entity_id=spec.entity_id,
            actor=spec.actor, system=spec.system, payload_includes=spec.payload_includes,
        )
    ]
    satisfied = count_satisfied(len(matching), spec.count)
    filters = {
        key: value for key, value in (
            ("entity_type", spec.entity_type), ("entity_id", spec.entity_id),
            ("actor", spec.actor), ("system", spec.system),
            ("payload_includes", spec.payload_includes),
        ) if value is not None
    }
    return RuleOutcome(
        satisfied=satisfied,
        detail=(
            f"{len(matching)} new {spec.type} event(s)"
            + (f" with {filters}" if filters else "")
            + f" (want {count_describe(spec.count)})"
        ),
        covered={event_atom_id(event) for event in matching} if satisfied else set(),
        evidence=[event_evidence(event) for event in matching],
    )
