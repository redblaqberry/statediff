"""compensated: every opening event in the suffix has exactly one matching
closing event at or after it (paired FIFO on a payload key), no closer without
an opener, and a net-state condition holds afterwards. Evaluated over the
suffix only: seeded history predates the event log by contract. Covers the
paired events when satisfied; expected-only.

Pairs events to EACH OTHER only. Both ends can agree on a payload key that
names no row at all, so tying either end to the entity it acts on is the
`correlated` rule's job, not this one's."""

from __future__ import annotations

from ..scenario import (
    CompensatedSpec,
    ScenarioError,
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
from ..diff import event_atom_id


def _pairing_key(value: object) -> tuple[str, object]:
    """The bucket an open/close payload value is queued under.

    Tagged with the value's TYPE because a dict lookup is `hash` plus `==`, and
    Python calls the integer 1 and the float 1.0 equal and hashes them alike:
    keyed by the bare payload value, an opener carrying 1 and a closer carrying
    1.0 paired, and a fully passing verdict certified a compensation between
    two events whose payloads disagree about what was opened and closed.
    `strict_equal` exists precisely so numeric types cannot silently reconcile,
    and this is that refusal in the shape a FIFO queue per key needs.
    `correlation_value` has already refused null, booleans, and non-scalars, so
    what arrives here is a string, an integer, or a float, and the type name
    separates all three.
    """
    return (type(value).__name__, value)


def evaluate(spec: CompensatedSpec, ctx: RuleContext) -> RuleOutcome:
    require_event_type(spec.id, spec.open_event.type)
    require_event_type(spec.id, spec.close_event.type)
    # The open and close types must differ, or the loop below reads every
    # matching event as an opener (the open branch wins over the close one) and
    # the pairing is incoherent. The model refuses equal types when it is built;
    # `model_construct`, `model_copy`, and plain assignment skip that, and with
    # no events of the type present the incoherence hides entirely: no opener,
    # no closer, and a satisfied net-state then reports PASS for a compensation
    # between an event type and itself.
    if spec.open_event.type == spec.close_event.type:
        raise ScenarioError(
            f"effect '{spec.id}': compensated open_event and close_event types must differ, "
            f"both are '{spec.open_event.type}'"
        )
    # Each selector's payload key is looked up in an event payload, so a lone
    # surrogate there reaches no key an artifact can carry. With no events of
    # the type present the pairing loop never exercises the key and a satisfied
    # net-state then reports "0 ... each closed" over a key nothing can hold.
    # The model refuses it; a spec mutated in Python skipped that.
    require_event_selector("open_event", spec.open_event)
    require_event_selector("close_event", spec.close_event)
    require_table(ctx, spec.id, spec.net_state.table)
    # The net-state selector is validated HERE and not left to `row_matches`,
    # which only sees the values a row actually reaches it with: over an empty
    # after-table no row does, and `expect_count: 0` then reports the pass an
    # uncomparable selector value made inevitable. A spec built or mutated in
    # Python never ran the model validator that refuses those values. It runs
    # BEFORE the column check because `require_columns` reads
    # `spec.net_state.where.keys()`: a `where` mutated to a non-mapping is
    # refused here rather than raising AttributeError past evaluate()'s handler.
    require_cell_values("net_state.where", spec.net_state.where)
    require_columns(ctx, spec.id, spec.net_state.table, spec.net_state.where.keys())
    # The count the selector is paired with, refused on the same terms: a
    # negative `expect_count` makes the net-state clause report a violation over
    # every world there is, so the rule fails the run for a number no table
    # could have produced rather than naming the scenario as the problem.
    require_count_spec("net_state.expect_count", spec.net_state.expect_count)

    open_by_key: dict[tuple[str, object], list] = {}
    paired = []
    violations: list[dict] = []
    opener_count = 0
    for event in ctx.diff.suffix_events:
        if event.type == spec.open_event.type:
            opener_count += 1
            key, violation = correlation_value(event, spec.open_event.payload_key, "open")
            if violation:
                violations.append(violation)
                continue
            open_by_key.setdefault(_pairing_key(key), []).append(event)
        elif event.type == spec.close_event.type:
            key, violation = correlation_value(event, spec.close_event.payload_key, "close")
            if violation:
                violations.append(violation)
                continue
            waiting = open_by_key.get(_pairing_key(key))
            if not waiting:
                violations.append({**event_evidence(event), "kind": "close_without_open"})
                continue
            paired.append((waiting.pop(0), event))

    for waiting in open_by_key.values():
        for event in waiting:
            violations.append({**event_evidence(event), "kind": "open_never_closed"})

    net_count = sum(
        1 for row in ctx.after_rows(spec.net_state.table) if row_matches(row, spec.net_state.where)
    )
    net_ok = net_count == spec.net_state.expect_count
    if not net_ok:
        violations.append({
            "kind": "net_state", "table": spec.net_state.table, "where": spec.net_state.where,
            "expected_count": spec.net_state.expect_count, "actual_count": net_count,
        })

    if violations:
        return RuleOutcome(
            satisfied=False,
            detail=(
                f"{spec.open_event.type}/{spec.close_event.type} compensation broken: "
                f"{len(violations)} violation(s), net {spec.net_state.table} count {net_count} "
                f"(want {spec.net_state.expect_count})"
            ),
            evidence=violations,
        )
    if opener_count == 0:
        # No opening event occurred, so the pairing invariant matched nothing. The
        # same reasoning as idempotent's empty selection: "every opener was closed"
        # holds vacuously over zero openers, and a satisfied net-state (an
        # `expect_count: 0`, say) then reports a clean compensation the run never
        # performed. A compensated effect is expected-only and asserts that
        # compensation actually happened, so an empty opener set verified nothing.
        return RuleOutcome(
            satisfied=False,
            detail=(
                f"no {spec.open_event.type} event occurred in the suffix, so this compensation "
                "paired nothing and verified nothing about the run (net "
                f"{spec.net_state.table} count {net_count})"
            ),
            evidence=[{"kind": "no_opening_event", "open_event": spec.open_event.type}],
        )
    covered = {event_atom_id(e) for pair in paired for e in pair}
    return RuleOutcome(
        satisfied=True,
        detail=(
            f"{len(paired)} {spec.open_event.type} event(s) each closed by {spec.close_event.type}; "
            f"net {spec.net_state.table} count {net_count} as required"
        ),
        covered=covered,
        evidence=[event_evidence(e) for pair in paired for e in pair],
    )
