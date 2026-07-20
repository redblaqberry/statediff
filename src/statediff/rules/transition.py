"""transition: a named row's field changed, optionally pinned at both ends.
Covers exactly its one field_changed atom when satisfied."""

from __future__ import annotations

from ..canonical import strict_equal
from ..diff import FieldChanged
from ..scenario import ScenarioError, TransitionSpec, require_cell_value, require_cell_values
from .base import RuleContext, RuleOutcome, require_columns, require_table


def evaluate(spec: TransitionSpec, ctx: RuleContext) -> RuleOutcome:
    require_table(ctx, spec.id, spec.table)
    # Everything TransitionSpec's model validator refuses, refused again here:
    # a spec built or mutated in Python never ran it, and nothing downstream
    # can stand in for it. The comparisons below answer "no atom matched",
    # which is indistinguishable from a real non-match, so a key or an endpoint
    # no cell could ever hold reported a FORBIDDEN transition as passing while
    # it was incapable of firing. The cardinality check is not cosmetic either:
    # an empty key would make the unpacking below raise StopIteration from
    # outside evaluate()'s handler, where every other unusable scenario answers
    # with an `error` verdict.
    if len(spec.key) != 1:
        raise ScenarioError(
            f"effect '{spec.id}': transition key must be exactly one column: value pair, "
            f"got {sorted(spec.key)}"
        )
    key_column, key_value = next(iter(spec.key.items()))
    require_columns(ctx, spec.id, spec.table, [key_column, spec.field])
    require_cell_values("key", spec.key)
    if spec.has_from:
        require_cell_value("from", spec.from_)
    if spec.has_to:
        require_cell_value("to", spec.to)
    pk = ctx.primary_key(spec.table)
    if key_column != pk:
        raise ScenarioError(
            f"effect '{spec.id}': transition key must use the primary key '{pk}' of {spec.table}, got '{key_column}'"
        )
    atom = next(
        (
            a for a in ctx.diff.atoms_for(spec.table)
            if isinstance(a, FieldChanged) and strict_equal(a.pk, key_value) and a.column == spec.field
        ),
        None,
    )
    if atom is None:
        # Same comparison as the atom lookup above and therefore the same
        # strictness: the row this names is what the detail below reports as
        # the field's current value, and a looser match would attribute one
        # row's value to a key that never identified it.
        after_row = next((r for r in ctx.after_rows(spec.table) if strict_equal(r[pk], key_value)), None)
        current = None if after_row is None else after_row.get(spec.field)
        return RuleOutcome(
            satisfied=False,
            detail=(
                f"{spec.table}[{key_value}].{spec.field} did not change"
                + (f" (currently {current!r})" if after_row is not None else " (row not present)")
            ),
        )
    endpoints_ok = (not spec.has_from or strict_equal(atom.before, spec.from_)) and (
        not spec.has_to or strict_equal(atom.after, spec.to)
    )
    if not endpoints_ok:
        return RuleOutcome(
            satisfied=False,
            detail=(
                f"{spec.table}[{key_value}].{spec.field} changed {atom.before!r} -> {atom.after!r}, "
                f"not the required transition"
            ),
            evidence=[atom.describe()],
        )
    return RuleOutcome(
        satisfied=True,
        detail=f"{spec.table}[{key_value}].{spec.field}: {atom.before!r} -> {atom.after!r}",
        covered={atom.atom_id},
        evidence=[atom.describe()],
    )
