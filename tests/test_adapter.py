"""Adapter validation: hash parity with SiloBench's published golden, and the
fail-closed error paths (tampering, counters, environment, table set, JSONL,
duplicate JSON keys)."""

import json

import pytest

from statediff.adapter import (
    ArtifactError,
    compute_hashes,
    cross_validate_event_log,
    load_event_log,
    load_pair,
    load_snapshot,
)
from tests.conftest import FIXTURES, SILOBENCH_SEED_GOLDEN, read_json, write_json

PAYMENT = FIXTURES / "baseline" / "payment"
HOLD = FIXTURES / "baseline" / "hold"


def rehash(raw: dict) -> dict:
    raw.update(compute_hashes(raw["meta"], raw["tables"]))
    return raw


def with_duplicate_key(raw: dict, key: str, first_value) -> str:
    """Serialize `raw` with `key` present twice at the top level: `first_value`
    first, the real value second. json.dumps cannot express that, so the two
    halves are spliced. Shared with tests/test_cli.py."""
    return "{" + json.dumps(key) + ":" + json.dumps(first_value) + "," + json.dumps(raw, ensure_ascii=False)[1:]


def test_seeded_baseline_matches_published_golden_hash():
    loaded = load_snapshot(PAYMENT / "before-snapshot.json")
    assert loaded.snapshot.state_hash == SILOBENCH_SEED_GOLDEN


def test_schema_v2_baseline_loads_and_matches_its_golden():
    loaded = load_snapshot(FIXTURES / "baseline" / "schema2" / "before-snapshot.json")
    assert loaded.schema_version == 2
    assert "vdw_vendor_risk_flags" in loaded.tables
    assert loaded.snapshot.state_hash == (
        "c6b1bcd35a594ddd20d5fdd98310c764db894ace9914b83ec53b4f0101b2cfa4"
    )


def test_cross_schema_pair_is_rejected():
    with pytest.raises(ArtifactError, match="different schema versions"):
        load_pair(
            PAYMENT / "before-snapshot.json",
            FIXTURES / "baseline" / "schema2" / "before-snapshot.json",
        )


def test_all_baselines_load_clean():
    for path in (
        PAYMENT / "before-snapshot.json",
        PAYMENT / "after-snapshot.json",
        HOLD / "before-snapshot.json",
        HOLD / "mid-snapshot.json",
        HOLD / "after-snapshot.json",
    ):
        loaded = load_snapshot(path)
        assert loaded.snapshot.format == "snapshot.v1"


def test_payment_after_events_parsed():
    loaded = load_snapshot(PAYMENT / "after-snapshot.json")
    assert [event.type for event in loaded.events] == ["PAYMENT_RELEASED"]
    assert loaded.events[0].payload["approval_id"] == "APR-0001"


def test_tampered_business_value_fails_hash_check(tmp_path):
    raw = read_json(PAYMENT / "after-snapshot.json")
    raw["tables"]["payments"][0]["amount_cents"] = 1
    path = tmp_path / "tampered.json"
    write_json(path, raw)
    with pytest.raises(ArtifactError, match="hash mismatch"):
        load_snapshot(path)


def test_stale_counter_fails_even_when_hashes_are_recomputed(tmp_path):
    raw = read_json(PAYMENT / "after-snapshot.json")
    for row in raw["tables"]["counters"]:
        if row["name"] == "payment":
            row["value"] = 0
    path = tmp_path / "stale-counter.json"
    write_json(path, rehash(raw))
    with pytest.raises(ArtifactError, match="counter payment"):
        load_snapshot(path)


def test_environment_meta_disagreement_fails(tmp_path):
    raw = read_json(PAYMENT / "before-snapshot.json")
    for row in raw["tables"]["environment"]:
        if row["key"] == "docs_outage":
            row["value"] = "1"
    path = tmp_path / "env-mismatch.json"
    write_json(path, rehash(raw))
    with pytest.raises(ArtifactError, match="environment"):
        load_snapshot(path)


def test_unknown_table_fails(tmp_path):
    raw = read_json(PAYMENT / "before-snapshot.json")
    raw["tables"]["shadow_ledger"] = []
    path = tmp_path / "unknown-table.json"
    write_json(path, raw)
    with pytest.raises(ArtifactError, match="unexpected table set"):
        load_snapshot(path)


def test_duplicate_primary_key_fails(tmp_path):
    raw = read_json(PAYMENT / "after-snapshot.json")
    raw["tables"]["payments"].append(dict(raw["tables"]["payments"][0]))
    path = tmp_path / "dup-pk.json"
    write_json(path, rehash(raw))
    with pytest.raises(ArtifactError, match="duplicate primary key"):
        load_snapshot(path)


def test_null_primary_key_fails(tmp_path):
    # Hash-consistent and otherwise well formed, but the payment has no
    # identity: it would still be diffed and counted while no scenario
    # selector and no audit event could ever name it.
    raw = read_json(PAYMENT / "after-snapshot.json")
    raw["tables"]["payments"][0]["payment_id"] = None
    path = tmp_path / "null-pk.json"
    write_json(path, rehash(raw))
    with pytest.raises(ArtifactError, match="null payment_id"):
        load_snapshot(path)


def test_event_log_cross_validation_accepts_the_real_export():
    loaded = load_snapshot(PAYMENT / "after-snapshot.json")
    log = load_event_log(PAYMENT / "after-events.jsonl")
    cross_validate_event_log(loaded, log, "after")


def test_event_log_actor_mismatch_fails(tmp_path):
    loaded = load_snapshot(PAYMENT / "after-snapshot.json")
    text = (PAYMENT / "after-events.jsonl").read_text(encoding="utf-8")
    tampered = tmp_path / "events.jsonl"
    tampered.write_text(text.replace("ap_approver", "ap_clerk"), encoding="utf-8")
    with pytest.raises(ArtifactError, match="differs between snapshot and event log"):
        cross_validate_event_log(loaded, load_event_log(tampered), "after")


def test_event_log_payload_number_type_change_fails(tmp_path):
    # The snapshot payload carries the integer 210000 and the JSONL export
    # carries 210000.0. Python compares them equal, so the cross-check accepted
    # a pair of artifacts whose payload types disagree and the bundle and the
    # verdict then reported the event log as cross-checked. Two independently
    # produced exports agreeing exactly is the entire content of this check.
    loaded = load_snapshot(PAYMENT / "after-snapshot.json")
    text = (PAYMENT / "after-events.jsonl").read_text(encoding="utf-8")
    tampered = tmp_path / "events.jsonl"
    tampered.write_text(text.replace('"amount_cents":210000', '"amount_cents":210000.0'), encoding="utf-8")
    log = load_event_log(tampered)
    assert isinstance(log.records[0].payload["amount_cents"], float)
    with pytest.raises(ArtifactError, match="differs between snapshot and event log"):
        cross_validate_event_log(loaded, log, "after")


def test_pair_rejects_backwards_mutation_seq():
    with pytest.raises(ArtifactError, match="lower mutation_seq"):
        load_pair(PAYMENT / "after-snapshot.json", PAYMENT / "before-snapshot.json")


def test_pair_rejects_different_seeds(tmp_path):
    raw = read_json(PAYMENT / "before-snapshot.json")
    raw["meta"]["seed"] = 4712
    for row in raw["tables"]["environment"]:
        if row["key"] == "seed":
            row["value"] = "4712"
    path = tmp_path / "other-seed.json"
    write_json(path, rehash(raw))
    with pytest.raises(ArtifactError, match="different seeds"):
        load_pair(path, PAYMENT / "after-snapshot.json")


def test_good_pair_loads_with_event_logs():
    pair = load_pair(
        PAYMENT / "before-snapshot.json",
        PAYMENT / "after-snapshot.json",
        PAYMENT / "before-events.jsonl",
        PAYMENT / "after-events.jsonl",
    )
    assert pair.before.snapshot.meta.mutation_seq == 0
    assert pair.after.snapshot.meta.mutation_seq == 1


def test_pair_records_whether_the_event_log_cross_check_ran():
    # The flags say what was actually verified, per side. A pair loaded without
    # an event log must not be indistinguishable from a cross-validated one.
    both = load_pair(
        PAYMENT / "before-snapshot.json", PAYMENT / "after-snapshot.json",
        PAYMENT / "before-events.jsonl", PAYMENT / "after-events.jsonl",
    )
    assert (both.before_events_cross_checked, both.after_events_cross_checked) == (True, True)
    after_only = load_pair(
        PAYMENT / "before-snapshot.json", PAYMENT / "after-snapshot.json",
        after_events_path=PAYMENT / "after-events.jsonl",
    )
    assert (after_only.before_events_cross_checked, after_only.after_events_cross_checked) == (False, True)
    neither = load_pair(PAYMENT / "before-snapshot.json", PAYMENT / "after-snapshot.json")
    assert (neither.before_events_cross_checked, neither.after_events_cross_checked) == (False, False)


# duplicate JSON object keys -------------------------------------------------

def test_duplicate_json_key_in_a_snapshot_is_rejected(tmp_path):
    raw = read_json(PAYMENT / "after-snapshot.json")
    path = tmp_path / "dup-key.json"
    path.write_text(with_duplicate_key(raw, "state_hash", "0" * 64), encoding="utf-8")
    # The blind spot being closed: last-wins parsing yields exactly the
    # untampered structure, so every recomputed hash still matches and the
    # tamper check cannot see the rewrite. Only the parser can.
    assert json.loads(path.read_text(encoding="utf-8")) == raw
    with pytest.raises(ArtifactError, match="duplicate JSON object key 'state_hash'"):
        load_snapshot(path)


def test_duplicate_json_key_in_an_event_payload_is_rejected(tmp_path):
    raw = read_json(PAYMENT / "after-snapshot.json")
    raw["tables"]["events"][0]["payload_json"] = (
        '{"payment_id":"PAY-0001","amount_cents":1,"amount_cents":210000,"approval_id":"APR-0001"}'
    )
    path = tmp_path / "dup-payload.json"
    write_json(path, rehash(raw))
    with pytest.raises(ArtifactError, match="duplicate JSON object key 'amount_cents'"):
        load_snapshot(path)


def test_non_finite_number_in_an_event_log_is_rejected(tmp_path):
    # `json.loads` decodes NaN and the infinities, which are Python extensions
    # and not JSON. The embedded snapshot payloads were already held to the
    # real JSON domain and this loader was not, so the exported `load_event_log`
    # handed a caller a value the format cannot carry, and NaN equals nothing
    # at all (itself included), so every later comparison against it answers
    # "these differ" while looking like a real disagreement.
    text = (PAYMENT / "after-events.jsonl").read_text(encoding="utf-8")
    for literal in ("NaN", "Infinity", "-Infinity"):
        path = tmp_path / f"{literal.strip('-')}-events.jsonl"
        path.write_text(text.replace('"amount_cents":210000', f'"amount_cents":{literal}'), encoding="utf-8")
        with pytest.raises(ArtifactError, match="JSON"):
            load_event_log(path)
    # The untouched export still loads, so what is refused is the value and
    # not the rewriting.
    assert len(load_event_log(PAYMENT / "after-events.jsonl").records) == 1


def test_a_lone_surrogate_anywhere_in_artifact_json_is_refused_at_the_parse(tmp_path):
    # `\ud800` written as a JSON escape is plain ASCII in the file and decodes
    # into a Python string with no UTF-8 encoding at all. Only embedded payloads
    # and event logs were held to the JSON domain, so a snapshot's OWN fields
    # slipped past: an escaped surrogate in `world_hash` reached the
    # hash-mismatch message, and a message carrying one cannot be written to
    # stdout, so the refusal arrived as an encoding traceback instead.
    def written(raw, name):
        path = tmp_path / name
        # ensure_ascii: the file itself stays valid UTF-8 and carries the
        # surrogate as the escape a tampered artifact would really contain.
        path.write_text(json.dumps(raw, ensure_ascii=True, indent=2), encoding="utf-8")
        return path

    raw = read_json(PAYMENT / "after-snapshot.json")
    raw["world_hash"] = "\ud800" + "0" * 59
    with pytest.raises(ArtifactError, match="outside the JSON/Unicode domain") as caught:
        load_snapshot(written(raw, "surrogate-hash.json"))
    # The whole point of refusing it at the parse boundary: everything
    # downstream quotes what it read, and a refusal nobody can write down is
    # not a refusal anyone receives.
    str(caught.value).encode("utf-8")

    # A cell, with the embedded hashes left untouched. The domain refusal names
    # the real problem, so it has to run BEFORE the hash comparison rather than
    # blame a mismatch that is only a consequence.
    raw = read_json(PAYMENT / "after-snapshot.json")
    raw["tables"]["invoices"][0]["status"] = "\ud800"
    with pytest.raises(ArtifactError, match="outside the JSON/Unicode domain"):
        load_snapshot(written(raw, "surrogate-cell.json"))

    # And an event log, which is parsed by a different loader through the same
    # entry point.
    text = (PAYMENT / "after-events.jsonl").read_text(encoding="utf-8")
    log = tmp_path / "surrogate-events.jsonl"
    log.write_text(text.replace('"actor":"ap_approver"', '"actor":"\\ud800"'), encoding="utf-8")
    with pytest.raises(ArtifactError, match="outside the JSON/Unicode domain"):
        load_event_log(log)

    # The untouched artifacts still load, so what is refused is the value and
    # not the rewriting.
    assert load_snapshot(PAYMENT / "after-snapshot.json").snapshot.format == "snapshot.v1"


def test_duplicate_json_key_in_an_event_log_is_rejected(tmp_path):
    # A log whose first `actor` reads ap_clerk and whose second reads
    # ap_approver: cross-validation compares the parsed record and would pass
    # while a human reading the file sees a different actor.
    lines = (PAYMENT / "after-events.jsonl").read_text(encoding="utf-8").splitlines()
    lines[1] = with_duplicate_key(json.loads(lines[1]), "actor", "ap_clerk")
    path = tmp_path / "dup-events.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ArtifactError, match="duplicate JSON object key 'actor'"):
        load_event_log(path)
