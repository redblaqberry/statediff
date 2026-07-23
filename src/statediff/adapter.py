"""SiloBench adapter: load and validate snapshot.v1 / event-log.v1 artifacts.

Everything here fails closed: any structural surprise, hash mismatch, or
internal inconsistency raises ArtifactError, which the engine reports as an
`error` verdict (never a pass).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .canonical import CanonicalError, canonical_json, sha256_hex, strict_equal
from .models import EventLog, EventLogHeader, EventRecord, Row, Snapshot


class ArtifactError(ValueError):
    """The artifact cannot be trusted; evaluation must return `error`."""


ADAPTER_NAME = "silobench"

# Primary keys per table, from the SiloBench DDL. Schema v2 renames the risk
# flags table (same columns); renamed columns inside vdw tables do not affect
# any primary key.
_PKS_COMMON: dict[str, str] = {
    "approval_requests": "approval_id",
    "counters": "name",
    "documents": "doc_id",
    "environment": "key",
    "erp_vendors": "erp_vendor_id",
    "events": "event_id",
    "holds": "hold_id",
    "invoices": "invoice_id",
    "payments": "payment_id",
    "purchase_orders": "po_id",
    "vdw_ap_invoices_export": "export_key",
    "vdw_bank_accounts": "account_key",
    "vdw_export_runs": "run_id",
    "vdw_vendors": "vendor_key",
}
PRIMARY_KEYS: dict[int, dict[str, str]] = {
    1: {**_PKS_COMMON, "vdw_risk_flags": "flag_key"},
    2: {**_PKS_COMMON, "vdw_vendor_risk_flags": "flag_key"},
}

# Full column sets per table, from the SiloBench DDL. Row shapes are validated
# against these at load time, and scenario selectors are validated against
# them at evaluation time (unknown column -> error, fail-closed).
_COLUMNS_COMMON: dict[str, frozenset[str]] = {
    "approval_requests": frozenset({
        "approval_id", "invoice_id", "requested_by", "reason", "status", "requested_ts", "completed_ts",
    }),
    "counters": frozenset({"name", "value"}),
    "documents": frozenset({
        "doc_id", "folder", "title", "doc_type", "vendor_name", "content", "acl_roles", "version", "created_date",
    }),
    "environment": frozenset({"key", "value"}),
    "erp_vendors": frozenset({"erp_vendor_id", "name", "status", "merged_into", "payment_terms_days"}),
    "events": frozenset({
        "event_id", "mutation_seq", "ts", "actor", "system", "type", "entity_type", "entity_id", "payload_json",
    }),
    "holds": frozenset({
        "hold_id", "invoice_id", "reason_code", "note", "active", "placed_by", "placed_ts", "released_by", "released_ts",
    }),
    "invoices": frozenset({
        "invoice_id", "erp_vendor_id", "po_id", "vendor_invoice_no", "amount_cents", "currency",
        "status", "received_date", "due_date",
    }),
    "payments": frozenset({"payment_id", "invoice_id", "amount_cents", "released_by", "approval_id", "released_ts"}),
    "purchase_orders": frozenset({"po_id", "erp_vendor_id", "amount_cents", "status"}),
    "vdw_export_runs": frozenset({"run_id", "exported_at", "schema_version"}),
}
_RISK_FLAG_COLUMNS = frozenset({"flag_key", "vendor_key", "flag_type", "severity", "as_of"})
COLUMNS: dict[int, dict[str, frozenset[str]]] = {
    1: {
        **_COLUMNS_COMMON,
        "vdw_vendors": frozenset({"vendor_key", "legal_name", "country", "erp_ref", "status"}),
        "vdw_bank_accounts": frozenset({
            "account_key", "vendor_key", "iban", "bic", "verification_status", "verified_at",
        }),
        "vdw_ap_invoices_export": frozenset({
            "export_key", "source_ref", "vendor_invoice_no", "vendor_key", "amount_eur", "status_as_of_export",
        }),
        "vdw_risk_flags": _RISK_FLAG_COLUMNS,
    },
    2: {
        **_COLUMNS_COMMON,
        "vdw_vendors": frozenset({"vendor_key", "vendor_legal_name", "country", "erp_ref", "status"}),
        "vdw_bank_accounts": frozenset({
            "account_key", "vendor_key", "iban_number", "bic", "verification_status", "verified_at",
        }),
        "vdw_ap_invoices_export": frozenset({
            "export_key", "source_ref", "vendor_invoice_no", "vendor_key", "amount_cents", "status_as_of_export",
        }),
        "vdw_vendor_risk_flags": _RISK_FLAG_COLUMNS,
    },
}

# events is represented solely by event atoms plus the append-only invariant;
# counters solely by the consistency invariants below. Neither participates in
# the generic table diff.
EVENTS_TABLE = "events"
BOOKKEEPING_TABLES = frozenset({"counters"})
ENVIRONMENT_TABLE = "environment"

_COUNTER_TABLES = {
    "payment": "payments",
    "hold": "holds",
    "approval": "approval_requests",
    "event": "events",
}

EVENT_TYPES = frozenset({
    "HOLD_PLACED",
    "HOLD_RELEASED",
    "APPROVAL_REQUESTED",
    "PAYMENT_RELEASED",
    "DUPLICATE_FLAGGED",
    "ACCESS_DENIED",
})


@dataclass(frozen=True)
class LoadedSnapshot:
    snapshot: Snapshot
    events: list[EventRecord]
    source: str

    @property
    def tables(self) -> dict[str, list[Row]]:
        return self.snapshot.tables

    @property
    def raw_events(self) -> list[Row]:
        """The stored event rows exactly as exported (payload as the raw
        payload_json string). The append-only invariant compares THESE, so a
        reformatted or key-reordered payload_json in history is a rewrite."""
        return self.snapshot.tables.get(EVENTS_TABLE, [])

    @property
    def schema_version(self) -> int:
        return self.snapshot.meta.schema_version

    def primary_keys(self) -> dict[str, str]:
        return PRIMARY_KEYS[self.schema_version]


@dataclass(frozen=True)
class ArtifactPair:
    before: LoadedSnapshot
    after: LoadedSnapshot
    # Whether each snapshot's events were actually cross-checked against an
    # independent event-log.v1 export. The event logs are optional inputs, so
    # False means the check never ran, which is NOT the same as passing it:
    # everything downstream reports these rather than let a consumer read an
    # unchecked pair as a validated one. Defaults are False so any pair built
    # without stating otherwise claims nothing.
    before_events_cross_checked: bool = False
    after_events_cross_checked: bool = False


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """json.loads' object hook: a repeated object key is a hard error.

    The default behaviour keeps the last value and discards the earlier one
    silently, which is invisible downstream because every hash here is computed
    from the PARSED structure: two byte-different files that differ only in a
    duplicated key canonicalize identically, so the hash comparison cannot see
    the rewrite. The scenario YAML loader already refuses duplicate keys for
    the same reason.
    """
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ArtifactError(f"duplicate JSON object key {key!r}")
        seen[key] = value
    return seen


def strict_json_loads(text: str, source: str) -> Any:
    """json.loads with duplicate object keys and non-JSON values rejected,
    blamed on `source`.

    The domain check belongs HERE, at the one point every artifact, embedded
    payload, event log, bundle manifest, and stored verdict is decoded, rather
    than at whichever call sites remembered it. A snapshot's own fields skipped
    it, so an escaped lone surrogate in `world_hash` survived into the
    hash-mismatch message below, and that message cannot be UTF-8 encoded:
    `check --json` died while writing the error verdict, so the one failure mode
    that promises a machine-readable refusal produced no verdict at all and an
    exit code reading as an ordinary failing run rather than an unevaluated one.
    """
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except ArtifactError as exc:
        raise ArtifactError(f"{source}: {exc}") from exc
    except RecursionError as exc:
        # A document nested thousands of arrays deep exhausts the parser's
        # stack, and RecursionError is a RuntimeError, not a JSONDecodeError,
        # so call sites catching decode errors never see it. Caught HERE for
        # the same reason the domain check lives here: the outer file, JSONL
        # record, and bundle manifest paths each grew their own catch, and the
        # payload_json string embedded in a snapshot's event rows then still
        # crashed with a traceback and exit 1 where a malformed artifact must
        # become an error verdict and exit 2. Every decode site inherits this
        # one.
        raise ArtifactError(f"{source}: JSON nested too deeply to parse") from exc
    try:
        _require_json_domain(source, value)
    except RecursionError as exc:
        # The domain walk and the parser spend different amounts of stack per
        # level, so a value the parser just barely decoded can still exhaust
        # the stack here.
        raise ArtifactError(f"{source}: JSON nested too deeply to parse") from exc
    return value


def _require_json_domain(what: str, value: Any) -> None:
    """Refuse anything Python's json module accepts that JSON itself does not.

    `json.loads` decodes the NaN/Infinity extensions by default, and a decoded
    lone surrogate has no UTF-8 encoding at all. NaN is not equal to itself, so
    a non-finite number that reached a verdict would make every comparison
    against it answer "these differ", which is indistinguishable from a real
    disagreement, and a lone surrogate poisons every message that quotes it: a
    verdict carrying one cannot be written to stdout, so the failure arrives as
    an encoding traceback instead of the machine-readable refusal statediff
    promises for every failure mode. Neither can legitimately appear in a v1
    artifact, and the publicly exported loaders must not hand a caller a value
    the format cannot carry.
    """
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeEncodeError) as exc:
        raise ArtifactError(f"{what} outside the JSON/Unicode domain: {exc}") from exc


def _read_json(path: Path) -> Any:
    try:
        return strict_json_loads(path.read_text(encoding="utf-8"), str(path))
    except FileNotFoundError as exc:
        raise ArtifactError(f"artifact not found: {path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"unparseable artifact {path}: {exc}") from exc
    except RecursionError as exc:
        # A JSON document nested thousands of arrays deep makes the parser exhaust
        # the interpreter stack. RecursionError is a RuntimeError, not a
        # JSONDecodeError, so without this it escaped as a traceback and exit 1
        # where a malformed artifact must become an `error` verdict and exit 2.
        raise ArtifactError(
            f"unparseable artifact {path}: JSON nested too deeply to parse"
        ) from exc


def compute_hashes(meta: dict[str, Any], tables: dict[str, list[Row]]) -> dict[str, str]:
    """Recompute the three SiloBench hashes from raw meta and tables.

    Public because the defect injector also uses it: derived artifacts must
    stay internally consistent so a defect is semantic, never file corruption.
    """
    world_meta = {
        "seed": meta["seed"],
        "schema_version": meta["schema_version"],
        "docs_outage": meta["docs_outage"],
    }
    non_world = {EVENTS_TABLE, *BOOKKEEPING_TABLES, ENVIRONMENT_TABLE}
    world_tables = {name: rows for name, rows in tables.items() if name not in non_world}
    state_meta = {**world_meta, "mutation_seq": meta["mutation_seq"]}
    return {
        "world_hash": sha256_hex(canonical_json({"meta": world_meta, "tables": world_tables})),
        "audit_hash": sha256_hex(canonical_json(tables.get(EVENTS_TABLE, []))),
        "state_hash": sha256_hex(canonical_json({"meta": state_meta, "tables": tables})),
    }


def _verify_hashes(snapshot: Snapshot, source: str) -> None:
    try:
        computed_all = compute_hashes(snapshot.meta.model_dump(), snapshot.tables)
    except CanonicalError as exc:
        raise ArtifactError(f"{source}: cannot canonicalize artifact: {exc}") from exc
    for name, computed, embedded in (
        ("world_hash", computed_all["world_hash"], snapshot.world_hash),
        ("audit_hash", computed_all["audit_hash"], snapshot.audit_hash),
        ("state_hash", computed_all["state_hash"], snapshot.state_hash),
    ):
        if computed != embedded:
            raise ArtifactError(
                f"{source}: {name} mismatch (embedded {embedded}, recomputed {computed}); "
                "artifact is corrupted or tampered"
            )


def _verify_table_set(snapshot: Snapshot, source: str) -> None:
    expected = set(PRIMARY_KEYS[snapshot.meta.schema_version])
    actual = set(snapshot.tables)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise ArtifactError(f"{source}: unexpected table set (missing {missing}, unknown {unknown})")


def _verify_keyed_rows(snapshot: Snapshot, source: str) -> None:
    pks = PRIMARY_KEYS[snapshot.meta.schema_version]
    columns = COLUMNS[snapshot.meta.schema_version]
    for table, rows in snapshot.tables.items():
        pk = pks[table]
        expected_columns = columns[table]
        seen: set[Any] = set()
        for row in rows:
            if set(row) != expected_columns:
                raise ArtifactError(
                    f"{source}: {table} row columns {sorted(row)} != DDL columns {sorted(expected_columns)}"
                )
            for column, cell in row.items():
                # SQLite exports carry only TEXT, INTEGER, and NULL. A JSON
                # boolean would collide with an integer under Python equality
                # while hashing differently, so it is rejected outright.
                if isinstance(cell, bool) or not isinstance(cell, (str, int, type(None))):
                    raise ArtifactError(
                        f"{source}: {table}.{column} has non-SQLite value {cell!r} "
                        f"({type(cell).__name__})"
                    )
            value = row[pk]
            # A primary key is the row's identity, and the source DDL declares
            # every one of them NOT NULL. A null is therefore a malformed
            # entity rather than a row with one field missing: the diff keys
            # rows by this value and every rule that names a row resolves
            # through it, so an unidentifiable row would still be diffed,
            # counted, and reported as though it were a real one.
            if value is None:
                raise ArtifactError(f"{source}: {table} has a row with a null {pk}")
            if value in seen:
                raise ArtifactError(f"{source}: {table} has duplicate primary key {value!r}")
            seen.add(value)


def _parse_events(snapshot: Snapshot, source: str) -> list[EventRecord]:
    events: list[EventRecord] = []
    for row in snapshot.tables.get(EVENTS_TABLE, []):
        row = dict(row)
        payload_json = row.pop("payload_json", None)
        if not isinstance(payload_json, str):
            raise ArtifactError(f"{source}: event row without payload_json string: {row}")
        try:
            payload = strict_json_loads(payload_json, f"{source}: event payload_json")
        except json.JSONDecodeError as exc:
            raise ArtifactError(f"{source}: event payload_json is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ArtifactError(f"{source}: event payload is not an object: {payload!r}")
        try:
            events.append(EventRecord(**row, payload=payload))
        except ValidationError as exc:
            raise ArtifactError(f"{source}: malformed event row: {exc}") from exc
    for previous, current in zip(events, events[1:]):
        if current.mutation_seq < previous.mutation_seq:
            raise ArtifactError(
                f"{source}: event {current.event_id} has mutation_seq {current.mutation_seq} "
                f"below its predecessor {previous.mutation_seq}; the log ordering contract is broken"
            )
    return events


def _counter_map(snapshot: Snapshot, source: str) -> dict[str, int]:
    counters: dict[str, int] = {}
    for row in snapshot.tables.get("counters", []):
        name, value = row.get("name"), row.get("value")
        if not isinstance(name, str) or not isinstance(value, int):
            raise ArtifactError(f"{source}: malformed counters row: {row}")
        counters[name] = value
    return counters


def _verify_counters(snapshot: Snapshot, events: list[EventRecord], source: str) -> None:
    counters = _counter_map(snapshot, source)
    expected_names = {"mutation_seq", *_COUNTER_TABLES}
    if set(counters) != expected_names:
        raise ArtifactError(f"{source}: counters names {sorted(counters)} != expected {sorted(expected_names)}")
    for counter, table in _COUNTER_TABLES.items():
        actual = len(snapshot.tables.get(table, []))
        if counters[counter] != actual:
            raise ArtifactError(
                f"{source}: counter {counter}={counters[counter]} inconsistent with {actual} rows in {table}"
            )
    max_event_seq = max((event.mutation_seq for event in events), default=0)
    if counters["mutation_seq"] < max_event_seq:
        raise ArtifactError(
            f"{source}: mutation_seq counter {counters['mutation_seq']} behind event mutation_seq {max_event_seq}"
        )
    if counters["mutation_seq"] != snapshot.meta.mutation_seq:
        raise ArtifactError(
            f"{source}: mutation_seq counter {counters['mutation_seq']} != meta.mutation_seq {snapshot.meta.mutation_seq}"
        )


def _verify_environment(snapshot: Snapshot, source: str) -> None:
    env = {row.get("key"): row.get("value") for row in snapshot.tables.get(ENVIRONMENT_TABLE, [])}
    meta = snapshot.meta
    expected = {
        "seed": str(meta.seed),
        "schema_version": str(meta.schema_version),
        "docs_outage": "1" if meta.docs_outage else "0",
    }
    if env != expected:
        raise ArtifactError(f"{source}: environment rows {env} disagree with meta {expected}")


def load_snapshot(path: str | Path) -> LoadedSnapshot:
    path = Path(path)
    raw = _read_json(path)
    try:
        snapshot = Snapshot.model_validate(raw)
    except ValidationError as exc:
        raise ArtifactError(f"{path}: not a valid snapshot.v1: {exc}") from exc
    source = str(path)
    _verify_table_set(snapshot, source)
    _verify_keyed_rows(snapshot, source)
    _verify_hashes(snapshot, source)
    events = _parse_events(snapshot, source)
    _verify_counters(snapshot, events, source)
    _verify_environment(snapshot, source)
    return LoadedSnapshot(snapshot=snapshot, events=events, source=source)


def load_event_log(path: str | Path) -> EventLog:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ArtifactError(f"event log not readable: {path}: {exc}") from exc
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ArtifactError(f"{path}: empty event log")
    try:
        header = EventLogHeader.model_validate(strict_json_loads(lines[0], f"{path}: header"))
        records = [
            # Numbered by record rather than by file line: blank lines are
            # dropped above, so the two do not necessarily agree.
            EventRecord.model_validate(strict_json_loads(line, f"{path}: record {number}"))
            for number, line in enumerate(lines[1:], start=1)
        ]
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ArtifactError(f"{path}: malformed event-log.v1: {exc}") from exc
    except RecursionError as exc:
        # A single JSONL record nested thousands of arrays deep exhausts the
        # parser's stack. RecursionError is not a JSONDecodeError, so without this
        # it escaped as a traceback and exit 1 where a malformed artifact must
        # become an error verdict and exit 2.
        raise ArtifactError(f"{path}: event-log record nested too deeply to parse") from exc
    if header.events != len(records):
        raise ArtifactError(f"{path}: header claims {header.events} events, found {len(records)}")
    for previous, current in zip(records, records[1:]):
        if current.mutation_seq < previous.mutation_seq:
            raise ArtifactError(
                f"{path}: event {current.event_id} has mutation_seq {current.mutation_seq} "
                f"below its predecessor {previous.mutation_seq}"
            )
    return EventLog(header=header, records=records)


def cross_validate_event_log(loaded: LoadedSnapshot, log: EventLog, source: str) -> None:
    """Full-field equality between the snapshot's events and the JSONL export.

    Equality here is TYPE-exact, down into the payload (`strict_equal`), because
    the entire value of this check is that two independently produced artifacts
    agree. A payload amount of `210000` in the snapshot and `210000.0` in the
    JSONL is a disagreement between the two exporters; accepting it would let a
    bundle and a verdict report the event log as cross-checked while the two
    files say different things.
    """
    meta = loaded.snapshot.meta
    header = log.header
    if (header.seed, header.schema_version, header.docs_outage) != (
        meta.seed, meta.schema_version, meta.docs_outage,
    ):
        raise ArtifactError(f"{source}: event log header profile disagrees with snapshot meta")
    if len(log.records) != len(loaded.events):
        raise ArtifactError(
            f"{source}: event log has {len(log.records)} events, snapshot has {len(loaded.events)}"
        )
    for snap_event, log_event in zip(loaded.events, log.records):
        snap_fields = snap_event.model_dump()
        log_fields = log_event.model_dump()
        if snap_fields.keys() != log_fields.keys() or not all(
            strict_equal(snap_fields[key], log_fields[key]) for key in snap_fields
        ):
            raise ArtifactError(
                f"{source}: event {snap_event.event_id} differs between snapshot and event log"
            )


def load_pair(
    before_path: str | Path,
    after_path: str | Path,
    before_events_path: str | Path | None = None,
    after_events_path: str | Path | None = None,
) -> ArtifactPair:
    before = load_snapshot(before_path)
    after = load_snapshot(after_path)
    if before.snapshot.meta.seed != after.snapshot.meta.seed:
        raise ArtifactError("before and after snapshots come from different seeds")
    if before.schema_version != after.schema_version:
        raise ArtifactError(
            "before and after snapshots have different schema versions; "
            "cross-schema diffing is out of scope for v0.1"
        )
    if after.snapshot.meta.mutation_seq < before.snapshot.meta.mutation_seq:
        raise ArtifactError("after snapshot has a lower mutation_seq than before")
    if before_events_path is not None:
        cross_validate_event_log(before, load_event_log(before_events_path), str(before_events_path))
    if after_events_path is not None:
        cross_validate_event_log(after, load_event_log(after_events_path), str(after_events_path))
    return ArtifactPair(
        before=before,
        after=after,
        before_events_cross_checked=before_events_path is not None,
        after_events_cross_checked=after_events_path is not None,
    )
