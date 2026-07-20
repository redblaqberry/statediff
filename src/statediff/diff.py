"""Structural diff between two loaded snapshots.

The generic table diff excludes `events` (represented solely as appended-event
atoms plus the append-only prefix invariant) and `counters` (governed by the
adapter's consistency invariants). `environment` IS diffed: profile changes
must be justified or they fail.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .adapter import BOOKKEEPING_TABLES, EVENTS_TABLE, ArtifactPair
from .models import EventRecord, Row

AtomId = tuple

FIELD = "field_changed"
ROW_ADDED = "row_added"
ROW_REMOVED = "row_removed"
EVENT = "event_appended"


@dataclass(frozen=True)
class FieldChanged:
    table: str
    pk: Any
    column: str
    before: Any
    after: Any

    @property
    def atom_id(self) -> AtomId:
        return (FIELD, self.table, self.pk, self.column)

    def describe(self) -> dict[str, Any]:
        return {
            "kind": FIELD, "table": self.table, "pk": self.pk,
            "column": self.column, "before": self.before, "after": self.after,
        }


@dataclass(frozen=True)
class RowAdded:
    table: str
    pk: Any
    row: Row

    @property
    def atom_id(self) -> AtomId:
        return (ROW_ADDED, self.table, self.pk)

    def describe(self) -> dict[str, Any]:
        return {"kind": ROW_ADDED, "table": self.table, "pk": self.pk, "row": self.row}


@dataclass(frozen=True)
class RowRemoved:
    table: str
    pk: Any
    row: Row

    @property
    def atom_id(self) -> AtomId:
        return (ROW_REMOVED, self.table, self.pk)

    def describe(self) -> dict[str, Any]:
        return {"kind": ROW_REMOVED, "table": self.table, "pk": self.pk, "row": self.row}


TableAtom = FieldChanged | RowAdded | RowRemoved


def event_atom_id(event: EventRecord) -> AtomId:
    return (EVENT, event.event_id)


@dataclass(frozen=True)
class Diff:
    atoms: list[TableAtom]
    suffix_events: list[EventRecord]
    append_only_ok: bool
    append_only_detail: str

    def atoms_for(self, table: str) -> list[TableAtom]:
        return [atom for atom in self.atoms if atom.table == table]


def _keyed(rows: list[Row], pk: str) -> dict[Any, Row]:
    return {row[pk]: row for row in rows}


def _sorted_keys(keys) -> list:
    # Deterministic order regardless of set-iteration order, so verdict
    # evidence is a pure function of the inputs across processes. The type
    # name breaks ties between values like 1 and "1".
    return sorted(keys, key=lambda value: (value is None, type(value).__name__, str(value)))


def compute_diff(pair: ArtifactPair) -> Diff:
    pks = pair.after.primary_keys()
    excluded = {EVENTS_TABLE, *BOOKKEEPING_TABLES}
    atoms: list[TableAtom] = []
    for table in sorted(pair.after.tables):
        if table in excluded:
            continue
        pk = pks[table]
        before_rows = _keyed(pair.before.tables[table], pk)
        after_rows = _keyed(pair.after.tables[table], pk)
        for key in _sorted_keys(after_rows.keys() - before_rows.keys()):
            atoms.append(RowAdded(table=table, pk=key, row=after_rows[key]))
        for key in _sorted_keys(before_rows.keys() - after_rows.keys()):
            atoms.append(RowRemoved(table=table, pk=key, row=before_rows[key]))
        for key in _sorted_keys(before_rows.keys() & after_rows.keys()):
            before_row, after_row = before_rows[key], after_rows[key]
            for column in sorted(before_row):
                if before_row[column] != after_row[column]:
                    atoms.append(FieldChanged(
                        table=table, pk=key, column=column,
                        before=before_row[column], after=after_row[column],
                    ))

    # The append-only invariant compares the RAW stored event rows (payload as
    # the payload_json string): reformatting or reordering keys inside a
    # historical payload_json changes the audit trail and must fail, even
    # though the parsed payload object would compare equal.
    before_raw = pair.before.raw_events
    after_raw = pair.after.raw_events
    append_only_ok = len(after_raw) >= len(before_raw) and after_raw[: len(before_raw)] == before_raw
    after_events = pair.after.events
    if append_only_ok:
        detail = f"{len(before_raw)} historical events intact, {len(after_raw) - len(before_raw)} appended"
        suffix = after_events[len(before_raw):]
    else:
        detail = (
            "audit history was rewritten or truncated: the before event log is not a prefix "
            f"of the after event log ({len(before_raw)} before, {len(after_raw)} after)"
        )
        suffix = after_events[len(before_raw):] if len(after_raw) > len(before_raw) else []
    return Diff(atoms=atoms, suffix_events=suffix, append_only_ok=append_only_ok, append_only_detail=detail)
