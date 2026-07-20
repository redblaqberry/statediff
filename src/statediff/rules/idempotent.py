"""idempotent (mode unique_effect): within the scoped after-table rows, no two
rows share the same key_fields tuple. This is the single-run form of
idempotency: a retried operation must not duplicate its effect. A supplied
`scope` that selects zero rows is a violation rather than a pass: uniqueness
over an empty set holds for every table that ever existed, so it verified
nothing about this one. Supplying the key is what decides, not what it
contains. Pure invariant: covers nothing, expected-only."""

from __future__ import annotations

from collections import defaultdict

from ..scenario import IdempotentSpec, ScenarioError, require_cell_values
from .base import RuleContext, RuleOutcome, require_columns, require_table, row_matches


def evaluate(spec: IdempotentSpec, ctx: RuleContext) -> RuleOutcome:
    require_table(ctx, spec.id, spec.table)
    # The model pins `mode` to its one supported value; a spec built or mutated in
    # Python skips that, and this rule reads only the `unique_effect` behaviour, so
    # any other mode would be evaluated AS unique_effect and could report a pass on
    # a mode this build does not implement. Refused in the same shape as the
    # in-Python guards below, which exist because model validation ran only when
    # the spec was first built.
    if spec.mode != "unique_effect":
        raise ScenarioError(
            f"effect '{spec.id}': idempotent mode {spec.mode!r} is not supported; "
            "'unique_effect' is the only mode this build implements"
        )
    # The model requires at least one key field; `model_construct`, `model_copy`,
    # and plain attribute assignment all skip that, and `require_columns` cannot
    # stand in for it because zero columns are trivially all known. With no
    # fields every row groups under the empty tuple, so this reports duplicates
    # whenever the table holds two rows and uniqueness whenever it holds one or
    # none: neither answer is about a key, and the passing one hands a consumer
    # an idempotency guarantee over nothing at all.
    if not spec.key_fields:
        raise ScenarioError(
            f"effect '{spec.id}': idempotent needs at least one key_field; uniqueness over "
            "zero fields only says the table holds at most one row, which is not a claim "
            "about any effect being duplicated"
        )
    require_columns(ctx, spec.id, spec.table, spec.key_fields)
    # Not left to `row_matches`, which only ever sees the values a row reaches
    # it with: over an empty after-table none does, so a spec built or mutated
    # in Python bypassed the refusal and reported the empty-selection violation
    # below as though the world were missing rows a comparable selector would
    # have found. It runs BEFORE the scope column check because `require_columns`
    # reads `spec.scope.keys()`: a `scope` mutated to a non-mapping is refused
    # here rather than raising AttributeError past evaluate()'s handler.
    require_cell_values("scope", spec.scope)
    # `scope` is tested for PRESENCE, not truthiness, everywhere below, exactly
    # as `unchanged.where` and `correlated.match` are: an explicit `scope: {}`
    # is a value the scenario author supplied and must take the same path as a
    # populated one, or reading it for truthiness drops it into the bare
    # whole-table branch that makes no claim at all.
    supplied = spec.scope is not None
    if supplied:
        require_columns(ctx, spec.id, spec.table, spec.scope.keys())

    scoped =[row for row in ctx.after_rows(spec.table) if row_matches(row, spec.scope)]
    scope_note = f" within scope {spec.scope}" if supplied else ""
    if supplied and not scoped:
        # A supplied scope asserts that the rows it names exist. Selecting none
        # means uniqueness was checked over an empty set, which holds for every
        # table in every world and therefore says nothing about this one, while
        # a consumer reading `passed` sees an idempotency guarantee. Only
        # omitting the key entirely makes no such claim, which is why the bare
        # whole-table form still passes over an empty table.
        return RuleOutcome(
            satisfied=False,
            detail=(
                f"{spec.table}{scope_note}: the selector matched no rows in the after snapshot, "
                "so this invariant verified nothing (omit `scope` to check the whole table)"
            ),
            evidence=[{"kind": "empty_selection", "table": spec.table, "scope": spec.scope}],
        )

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in scoped:
        groups[tuple(row[field] for field in spec.key_fields)].append(row)
    duplicates = {key: rows for key, rows in groups.items() if len(rows) > 1}

    if duplicates:
        evidence = [
            {"kind": "duplicate_group", "table": spec.table, "key_fields": spec.key_fields,
             "key": list(key), "rows": rows}
            for key, rows in duplicates.items()
        ]
        total = sum(len(rows) for rows in duplicates.values())
        return RuleOutcome(
            satisfied=False,
            detail=(
                f"{spec.table}{scope_note}: {total} rows share {len(duplicates)} "
                f"{'/'.join(spec.key_fields)} value(s); the effect was duplicated"
            ),
            evidence=evidence,
        )
    return RuleOutcome(
        satisfied=True,
        detail=f"{spec.table}{scope_note}: every {'/'.join(spec.key_fields)} tuple is unique",
    )
