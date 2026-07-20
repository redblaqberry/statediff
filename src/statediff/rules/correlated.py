"""correlated: the rows a selector names and the audit events that reference
them are the SAME entities.

Every other rule here checks rows and events in separate universes: a scenario
can require "one payment row matching this invoice" and "one PAYMENT_RELEASED
event carrying this approval id" and never state that the event audits that
row. Both hold when the row is one payment and the event names another, so an
unaudited payment satisfies the scenario. This rule is the join:

- forward: each selected row must be named by the required number of suffix
  events of the given type (payload key equal to the row's primary key);
- reverse: no suffix event of that type may name a row that does not exist.

The reverse direction is checked against the WHOLE table rather than the
selected subset, so a run that legitimately touches other rows of the same
table is not flagged for them; what it refuses is a dangling audit reference.

Pure invariant: covers nothing, expected-only. The rows and events still need
their own expected effects, so declaring a correlation can never launder a
change past the sweep.
"""

from __future__ import annotations

from ..canonical import strict_equal
from ..scenario import (
    CorrelatedSpec,
    count_describe,
    count_satisfied,
    require_cell_values,
    require_count_spec,
    require_event_selector,
)
from .base import (
    RuleContext,
    RuleOutcome,
    correlation_value,
    event_evidence,
    require_columns,
    require_event_type,
    require_table,
    row_matches,
)


def evaluate(spec: CorrelatedSpec, ctx: RuleContext) -> RuleOutcome:
    require_table(ctx, spec.id, spec.table)
    # `row_matches` below only validates the values a row actually reaches it
    # with, so over an empty after-table a spec built or mutated in Python was
    # never checked at all and the empty-selection violation reported a missing
    # correspondence instead of an impossible selector. It runs BEFORE the
    # column check because `require_columns` reads `spec.match.keys()`: a `match`
    # mutated to a non-mapping is refused here rather than raising AttributeError
    # past evaluate()'s handler.
    require_cell_values("match", spec.match)
    if spec.match is not None:
        require_columns(ctx, spec.id, spec.table, spec.match.keys())
    # The per-row bound needs the same treatment for the same reason: a count no
    # row can be audited (a negative one, or bounds that cross) makes every
    # selected row read as uncorrelated, so the rule blames the world for a
    # requirement that was unmeetable before a single event was read.
    require_count_spec("count", spec.count)
    require_event_type(spec.id, spec.event.type)
    # The payload key has no registry the way the event type does, so a lone
    # surrogate there is looked up in every payload, matches no key an artifact
    # can carry, and makes every selected row read as uncorrelated. The model
    # refuses it; a spec mutated in Python skipped that.
    require_event_selector("event", spec.event)

    pk = ctx.primary_key(spec.table)
    payload_key = spec.event.payload_key
    rows = [row for row in ctx.after_rows(spec.table) if row_matches(row, spec.match)]
    # Presence, not truthiness: an explicit `match: {}` is something the author
    # wrote and the report says so. It selects every row, and an empty
    # selection is a violation here either way, so it opens no bypass.
    match_note = f" matching {spec.match}" if spec.match is not None else ""
    subject = f"{spec.table}{match_note} <-> {spec.event.type}.{payload_key}"

    if not rows:
        # No rows means the per-row count clause ran zero times, so the rule
        # asserted nothing at all. Unlike `unchanged`, this rule has no
        # blanket-guard reading under which an empty table is a real result:
        # a correspondence with nothing on one side is not a correspondence.
        return RuleOutcome(
            satisfied=False,
            detail=(
                f"{subject}: the selector matched no rows in the after snapshot, "
                f"so no {spec.event.type} correlation was verified"
            ),
            evidence=[{"kind": "empty_selection", "table": spec.table, "match": spec.match}],
        )

    # Indexed once and read by both directions, so the two can never disagree
    # about which event names which row.
    correlations: list[tuple] = []
    event_violations: list[dict] = []
    events = [event for event in ctx.diff.suffix_events if event.type == spec.event.type]
    for event in events:
        value, violation = correlation_value(event, payload_key, "event")
        if violation:
            event_violations.append(violation)
            continue
        correlations.append((value, event))

    row_entries: list[dict] = []
    uncorrelated_rows = 0
    for row in rows:
        matched = [event for value, event in correlations if strict_equal(value, row[pk])]
        satisfied_here = count_satisfied(len(matched), spec.count)
        if not satisfied_here:
            uncorrelated_rows += 1
        row_entries.append({
            "kind": "correlated" if satisfied_here else "row_not_correlated",
            "table": spec.table, "pk": row[pk], "row": row,
            "event_type": spec.event.type, "payload_key": payload_key,
            "event_ids": [event.event_id for event in matched],
            "want": count_describe(spec.count),
        })

    known = [row[pk] for row in ctx.after_rows(spec.table)]
    for value, event in correlations:
        if not any(strict_equal(value, key) for key in known):
            event_violations.append({
                **event_evidence(event), "kind": "event_names_missing_row",
                "table": spec.table, "payload_key": payload_key, "value": value,
            })

    # Row entries first and in after-table order, violations of both kinds
    # included: the human report renders the whole correspondence from this
    # list, so what a reader sees is exactly what was checked.
    # "not resolving to a row" covers all three event-side violations at once:
    # a missing payload key, an unusable value, and a value naming no row all
    # leave the event pointing at nothing.
    evidence = row_entries + event_violations
    if uncorrelated_rows or event_violations:
        return RuleOutcome(
            satisfied=False,
            detail=(
                f"{subject}: {uncorrelated_rows}/{len(rows)} row(s) not audited "
                f"{count_describe(spec.count)} time(s), "
                f"{len(event_violations)}/{len(events)} event(s) not resolving to a row"
            ),
            evidence=evidence,
        )
    return RuleOutcome(
        satisfied=True,
        detail=(
            f"{subject}: all {len(rows)} row(s) audited {count_describe(spec.count)} time(s), "
            f"all {len(events)} event(s) resolve to a row"
        ),
        evidence=evidence,
    )
