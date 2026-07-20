"""count_delta: bounded counts of added/removed rows matching a selector.
There is deliberately no net delta: additions and removals are counted
separately, so an add-plus-remove cannot cancel out. Covers the rows it
counted when its bounds are satisfied.

A selector that matches nothing is NOT a violation here, unlike the invariant
rules: the bound is an assertion the author wrote down and it is checked either
way, so `added: 0` over an empty match set is a measured result rather than a
predicate that quietly ran over nothing. What protects this rule instead is
that a selector value a cell can never hold is refused when the scenario is
parsed, because that is a selector which cannot match rather than one which
did not."""

from __future__ import annotations

from ..diff import RowAdded, RowRemoved
from ..scenario import (
    CountDeltaSpec,
    ScenarioError,
    count_describe,
    count_satisfied,
    require_cell_values,
    require_count_spec,
)
from .base import RuleContext, RuleOutcome, require_columns, require_table, row_matches


def evaluate(spec: CountDeltaSpec, ctx: RuleContext) -> RuleOutcome:
    require_table(ctx, spec.id, spec.table)
    # The cell-domain refusal is a model validator too, repeated here because a
    # spec built or mutated in Python never ran it, and it comes BEFORE the
    # column check: `require_columns` reads `spec.match.keys()`, so a `match`
    # mutated to a non-mapping would raise AttributeError past evaluate()'s
    # handler unless the shape is refused first. `row_matches` cannot stand in
    # for the value refusal either, since it sees a selector value only when a
    # row reaches it, so a forbidden effect over a table with NO candidate atoms
    # validated nothing, counted zero, and reported the pass its own selector
    # made inevitable.
    require_cell_values("match", spec.match)
    # Presence, not truthiness, the same way every other rule reads its
    # selector: an explicit `match: {}` is a value the author supplied and the
    # report says so, even though a conjunction of zero conditions selects
    # every row and so counts exactly what an omitted `match` counts.
    if spec.match is not None:
        require_columns(ctx, spec.id, spec.table, spec.match.keys())
    # A spec carrying neither bound measures counts and then asserts nothing
    # about them, so `satisfied` would say only that no bound was broken. The
    # model refuses it at construction; a spec mutated in Python skipped that.
    if spec.added is None and spec.removed is None:
        raise ScenarioError(
            f"effect '{spec.id}': count_delta needs at least one of added/removed, "
            "or it asserts nothing about the counts it takes"
        )
    # A bound no count can meet is the same nothing said the other way round: it
    # is checked, it is honest about what it counted, and it can only ever
    # disagree. `added: -1` under FORBIDDEN reports the pass it guaranteed
    # itself, and `{min: 2, max: 1}` does the same without a negative number in
    # sight. Only the model refuses these when the file is parsed.
    for name, bound in (("added", spec.added), ("removed", spec.removed)):
        if bound is not None:
            require_count_spec(name, bound)

    added = [
        a for a in ctx.diff.atoms_for(spec.table)
        if isinstance(a, RowAdded) and row_matches(a.row, spec.match)
    ]
    removed = [
        a for a in ctx.diff.atoms_for(spec.table)
        if isinstance(a, RowRemoved) and row_matches(a.row, spec.match)
    ]

    parts: list[str] = []
    satisfied = True
    covered = set()
    evidence = []
    if spec.added is not None:
        ok = count_satisfied(len(added), spec.added)
        satisfied = satisfied and ok
        parts.append(f"added {len(added)} (want {count_describe(spec.added)})")
        covered |= {a.atom_id for a in added}
        evidence += [a.describe() for a in added]
    if spec.removed is not None:
        ok = count_satisfied(len(removed), spec.removed)
        satisfied = satisfied and ok
        parts.append(f"removed {len(removed)} (want {count_describe(spec.removed)})")
        covered |= {a.atom_id for a in removed}
        evidence += [a.describe() for a in removed]

    match_note = f" matching {spec.match}" if spec.match is not None else ""
    return RuleOutcome(
        satisfied=satisfied,
        detail=f"{spec.table}{match_note}: " + ", ".join(parts),
        covered=covered if satisfied else set(),
        evidence=evidence,
    )
