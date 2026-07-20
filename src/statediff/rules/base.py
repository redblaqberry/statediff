"""Shared rule machinery: evaluation context, outcomes, selector validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..adapter import COLUMNS, EVENT_TYPES, PRIMARY_KEYS, ArtifactPair
from ..canonical import strict_equal
from ..diff import AtomId, Diff
from ..models import EventRecord, Row
from ..scenario import ScenarioError, require_cell_values


@dataclass(frozen=True)
class RuleContext:
    pair: ArtifactPair
    diff: Diff

    @property
    def schema_version(self) -> int:
        return self.pair.after.schema_version

    def columns(self, table: str) -> frozenset[str]:
        return COLUMNS[self.schema_version][table]

    def primary_key(self, table: str) -> str:
        return PRIMARY_KEYS[self.schema_version][table]

    def after_rows(self, table: str) -> list[Row]:
        return self.pair.after.tables[table]

    def before_rows(self, table: str) -> list[Row]:
        return self.pair.before.tables[table]


@dataclass(frozen=True)
class RuleOutcome:
    """Result of evaluating one effect. `satisfied` is the rule's predicate;
    the engine interprets it per list (expected: must hold; forbidden: must
    not). `covered` justifies atoms only when the engine accepts the outcome
    (conditional coverage)."""

    satisfied: bool
    detail: str
    covered: set[AtomId] = field(default_factory=set)
    evidence: list[dict[str, Any]] = field(default_factory=list)


_RULE_EXCLUDED_TABLES = frozenset({"counters", "events"})


def require_table(ctx: RuleContext, rule_id: str, table: str) -> None:
    # `table` is quoted with `!r`, not bare, so a table mutated to a lone
    # surrogate names itself in an ASCII-safe form: a bare `'{table}'` put the
    # unencodable value straight into this message, and the error verdict built
    # from it then could not be written to stdout at all.
    if table in _RULE_EXCLUDED_TABLES:
        raise ScenarioError(
            f"effect '{rule_id}': table {table!r} is not addressable by table rules; "
            "events are governed by event rules and the append-only invariant, "
            "counters by the adapter consistency invariants"
        )
    if table not in COLUMNS[ctx.schema_version]:
        raise ScenarioError(
            f"effect '{rule_id}': unknown table {table!r} for schema v{ctx.schema_version} "
            f"(known: {sorted(COLUMNS[ctx.schema_version])})"
        )


def require_columns(ctx: RuleContext, rule_id: str, table: str, columns: Any) -> None:
    known = ctx.columns(table)
    unknown = [column for column in columns if column not in known]
    if unknown:
        raise ScenarioError(
            f"effect '{rule_id}': unknown columns {unknown} on '{table}' (known: {sorted(known)})"
        )


def require_event_type(rule_id: str, event_type: str) -> None:
    # `event_type` is quoted with `!r`, not bare: an event type mutated to a
    # lone surrogate has no UTF-8 encoding, and a bare `'{event_type}'` carried
    # it into this message, so the error verdict built from the refusal could
    # not be written and the promised machine-readable output never appeared.
    if event_type not in EVENT_TYPES:
        raise ScenarioError(
            f"effect '{rule_id}': unknown event type {event_type!r} (known: {sorted(EVENT_TYPES)})"
        )


def row_matches(row: Row, where: dict[str, Any] | None) -> bool:
    """The selector syntax: a conjunction of column equality tests, evaluated
    against one row.

    EVERY value is checked against the snapshot cell domain first, before any
    comparison runs, so the answer cannot depend on which column happens to
    differ first. `parse_scenario` already refuses these values; this is the
    same refusal for a spec built in Python, and it is a refusal rather than a
    comparison on purpose. What this function declines to do is return False
    for a value no cell could ever hold: a caller reading that False cannot
    tell "this row is not the one" from "no row can ever be", and the second
    silently satisfies a forbidden effect.
    """
    if not where:
        return True
    require_cell_values("selector", where)
    return all(strict_equal(row.get(column), value) for column, value in where.items())


def event_matches(event: EventRecord, *, type_: str, entity_type: str | None = None,
                  entity_id: str | None = None, actor: str | None = None,
                  system: str | None = None, payload_includes: dict[str, Any] | None = None) -> bool:
    if event.type != type_:
        return False
    if entity_type is not None and event.entity_type != entity_type:
        return False
    if entity_id is not None and event.entity_id != entity_id:
        return False
    if actor is not None and event.actor != actor:
        return False
    if system is not None and event.system != system:
        return False
    # Presence, not truthiness: an explicit `payload_includes: {}` is a real
    # zero-pin filter the author wrote (a conjunction of no pins matches every
    # event), and it must take the same path a populated one does. Read for
    # truthiness it fell through as though absent, so a spec whose pins were
    # emptied still reported the events as matched while checking none of them.
    if payload_includes is not None:
        for key, value in payload_includes.items():
            if key not in event.payload or not strict_equal(event.payload[key], value):
                return False
    return True


def event_evidence(event: EventRecord) -> dict[str, Any]:
    return {
        "kind": "event", "event_id": event.event_id, "type": event.type,
        "mutation_seq": event.mutation_seq, "ts": event.ts,
        "actor": event.actor, "system": event.system,
        "entity_type": event.entity_type, "entity_id": event.entity_id,
        "payload": event.payload,
    }


def correlation_value(event: EventRecord, payload_key: str, kind_prefix: str):
    """A usable correlation value from an event payload, or a violation entry.

    Missing keys, null values, and non-scalar values cannot correlate anything:
    a missing key and a null one would make every event carrying the same gap
    correlate with every other, and a dict/list/bool never equals a SQLite
    cell, so accepting either would over-match or match nothing at all. Shared
    by every rule that joins events to each other or to rows, so all of them
    refuse the same values for the same reason.
    """
    if payload_key not in event.payload:
        return None, {**event_evidence(event), "kind": f"{kind_prefix}_missing_key"}
    value = event.payload[payload_key]
    if value is None or isinstance(value, (dict, list, bool)):
        return None, {
            **event_evidence(event), "kind": f"{kind_prefix}_unusable_key",
            "payload_key": payload_key, "value": value,
        }
    return value, None
