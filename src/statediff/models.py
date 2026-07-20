"""Pydantic models for the artifacts StateDiff consumes and produces."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Row = dict[str, Any]


class SnapshotMeta(BaseModel):
    # strict: a string "4711" or numeric docs_outage must be rejected, not
    # coerced; hashes are recomputed from these values and coercion would
    # accept artifacts whose raw types disagree with their hashes.
    model_config = ConfigDict(extra="forbid", strict=True)
    seed: int
    schema_version: Literal[1, 2]
    docs_outage: bool
    mutation_seq: int = Field(ge=0)


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["snapshot.v1"]
    meta: SnapshotMeta
    tables: dict[str, list[Row]]
    world_hash: str
    audit_hash: str
    state_hash: str


class EventRecord(BaseModel):
    """One domain event, in the parsed form event-log.v1 uses."""

    model_config = ConfigDict(extra="forbid", strict=True)
    event_id: str
    mutation_seq: int = Field(ge=0)
    ts: str
    actor: str
    system: str
    type: str
    entity_type: str
    entity_id: str
    payload: dict[str, Any]


class EventLogHeader(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    format: Literal["event-log.v1"]
    seed: int
    schema_version: Literal[1, 2]
    docs_outage: bool
    events: int = Field(ge=0)


class EventLog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    header: EventLogHeader
    records: list[EventRecord]


class CheckOutcome(BaseModel):
    """One named check in a verdict; the shape agent-eval-gate style runners
    consume (name/passed/detail) is derived from this in gate.py.

    `not_applicable` exists because an allowed effect is never required: one
    that matched nothing has justified nothing, and calling that `pass` claims
    a predicate held when it did not. It is not a failure either, so it is its
    own third state rather than a lie in one direction or the other."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    id: str
    rule: str
    list_name: Literal["expected", "allowed", "forbidden", "invariant", "artifact"] = Field(alias="list")
    status: Literal["pass", "fail", "not_applicable"]
    detail: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class ArtifactFingerprints(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int
    before_state_hash: str
    after_state_hash: str
    # Event counts read from the snapshots' own events table. They are NOT
    # evidence that anything was cross-checked, which is why the two flags
    # below sit beside them: an event log is an optional input, and a bundle
    # captured without one must not read as a cross-validated bundle. Absent
    # in an older stored verdict means the check did not run.
    before_events: int
    after_events: int
    before_events_cross_checked: bool = False
    after_events_cross_checked: bool = False


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["statediff.verdict.v1"] = "statediff.verdict.v1"
    scenario_id: str
    title: str | None = None
    status: Literal["pass", "fail", "error"]
    error: str | None = None
    checks: list[CheckOutcome] = Field(default_factory=list)
    unexplained: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: ArtifactFingerprints | None = None
    provenance: dict[str, Any] | None = None

    def to_json(self) -> str:
        import json

        return json.dumps(self.model_dump(by_alias=True), ensure_ascii=False, indent=2)
