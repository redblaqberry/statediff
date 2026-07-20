"""unchanged: rows selected in the BEFORE snapshot must exist unmodified in
the after snapshot. Rows entering the subset only in after are additions and
fall to the global sweep. A supplied `where` that selects zero rows is a
violation rather than a pass: it verified nothing. Supplying the key is what
decides, not what it contains. Pure invariant: covers nothing,
expected-only."""

from __future__ import annotations

from ..diff import FieldChanged, RowRemoved
from ..scenario import UnchangedSpec, require_cell_values
from .base import RuleContext, RuleOutcome, require_columns, require_table, row_matches


def evaluate(spec: UnchangedSpec, ctx: RuleContext) -> RuleOutcome:
    require_table(ctx, spec.id, spec.table)
    # Refused here rather than only inside `row_matches`, which sees a selector
    # value only when a row reaches it: over an empty before-table none does,
    # so a spec built or mutated in Python skipped the check entirely and
    # answered with the empty-selection violation below, blaming the world for
    # a value that could never have matched anything. It runs BEFORE the column
    # check because `require_columns` reads `spec.where.keys()`: a `where`
    # mutated to a non-mapping is refused here rather than raising AttributeError
    # past evaluate()'s handler.
    require_cell_values("where", spec.where)
    # `where` is tested for PRESENCE, not truthiness, everywhere below. An
    # explicit `where: {}` is an input-controlled value like any other and must
    # take the same path as a populated one; reading it for truthiness let it
    # slip into the bare whole-table branch and pass over an empty table.
    supplied = spec.where is not None
    if supplied:
        require_columns(ctx, spec.id, spec.table, spec.where.keys())
    pk = ctx.primary_key(spec.table)
    selected = {row[pk] for row in ctx.before_rows(spec.table) if row_matches(row, spec.where)}

    where_note = f" where {spec.where}" if supplied else ""
    if supplied and not selected:
        # Supplying a `where` asserts that what it names exists; matching
        # nothing means the invariant froze an empty set, and a consumer
        # reading `passed` cannot tell that from a real pass. Only omitting the
        # key entirely makes no such assertion, which is why an empty table
        # under the bare whole-table form stays a pass.
        return RuleOutcome(
            satisfied=False,
            detail=(
                f"{spec.table}{where_note}: the selector matched no rows in the before snapshot, "
                "so this invariant verified nothing (omit `where` to freeze the whole table)"
            ),
            evidence=[{"kind": "empty_selection", "table": spec.table, "where": spec.where}],
        )

    violations = [
        atom for atom in ctx.diff.atoms_for(spec.table)
        if isinstance(atom, (FieldChanged, RowRemoved)) and atom.pk in selected
    ]
    if violations:
        return RuleOutcome(
            satisfied=False,
            detail=f"{spec.table}{where_note}: {len(violations)} change(s) to rows required to stay unchanged",
            evidence=[atom.describe() for atom in violations],
        )
    return RuleOutcome(
        satisfied=True,
        detail=f"{spec.table}{where_note}: all {len(selected)} selected row(s) unchanged",
    )
