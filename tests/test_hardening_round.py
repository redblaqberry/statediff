"""Regression guards for a round of fail-open and crash findings: in-memory
spec-field mutations that bypassed evaluation, an invariant that passed over an
empty match set, an allowed effect projected as a verified check with no
evidence, deeply nested inputs that escaped as tracebacks, and an error-verdict
builder that crashed on a null provenance.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from statediff.adapter import ArtifactError, load_pair
from statediff.engine import evaluate
from statediff.scenario import ScenarioError, load_scenario
from tests.conftest import FIXTURES
from tests.test_rules import check_by_id, clean_pair, scenario_with

SCENARIOS = FIXTURES.parent / "scenarios"
PAYMENT = FIXTURES / "baseline" / "payment"


# --- in-memory spec-field mutations must fail closed -----------------------

def test_idempotent_mode_mutated_in_python_is_error():
    sc = load_scenario(SCENARIOS / "payment-release.yaml")
    idem = next(e for e in sc.effects if e.rule == "idempotent")
    idem.spec.mode = "unsupported_mode"
    v = evaluate(sc, clean_pair("payment"))
    assert v.status == "error"
    assert "mode" in v.error


def test_event_payload_key_mutated_to_nonstring_is_error():
    sc = load_scenario(SCENARIOS / "hold-compensation.yaml")
    comp = next(e for e in sc.effects if e.rule == "compensated")
    comp.spec.open_event.payload_key = 123
    v = evaluate(sc, clean_pair("hold"))
    assert v.status == "error"
    assert "payload_key" in v.error


# --- an invariant that matched nothing verified nothing --------------------

def test_compensated_with_no_opening_event_does_not_pass_vacuously():
    comp = {"compensated": {
        "id": "comp",
        "open_event": {"type": "HOLD_PLACED", "payload_key": "hold_id"},
        "close_event": {"type": "HOLD_RELEASED", "payload_key": "hold_id"},
        # net_state selects nothing, so the net clause is satisfied (0 == 0); the
        # only thing that could clear this effect is a real opening event.
        "net_state": {"table": "holds", "where": {"hold_id": "NONEXISTENT-HOLD"},
                      "expect_count": 0}}}
    sc = scenario_with({"expected": [comp]})
    v = evaluate(sc, clean_pair("payment"))  # payment run records no HOLD_PLACED
    assert check_by_id(v, "comp").status == "fail"
    assert v.status != "pass"


# --- an allowed effect that fired nothing is not a verified check -----------

def test_allowed_effect_satisfied_by_absence_is_not_applicable():
    sc = scenario_with({
        "expected": [{"event_exists": {"id": "paid", "type": "PAYMENT_RELEASED",
                                        "count": {"min": 1}}}],
        "allowed": [{"event_exists": {"id": "zero-holds", "type": "HOLD_PLACED",
                                      "count": 0}}],
    })
    v = evaluate(sc, clean_pair("payment"))
    assert check_by_id(v, "zero-holds").status == "not_applicable"
    # And it must not project into the gate as a passed check.
    from statediff.gate import to_gate_checks
    projected = {c["name"]: c["passed"] for c in to_gate_checks(v)}
    assert "statediff:zero-holds" not in projected or projected["statediff:zero-holds"] is not True


# --- deeply nested inputs fail closed, never as a traceback -----------------

def test_deeply_nested_snapshot_is_artifact_error(tmp_path):
    from statediff.adapter import _read_json
    deep = tmp_path / "deep.json"
    depth = 12000
    deep.write_text("[" * depth + "]" * depth, encoding="utf-8")
    with pytest.raises(ArtifactError):
        _read_json(deep)


def test_deeply_nested_scenario_yaml_is_scenario_error(tmp_path):
    deep = tmp_path / "deep.yaml"
    depth = 12000
    deep.write_text(
        "scenario: statediff.scenario.v1\nid: x\ntitle: x\nadapter: silobench\n"
        "effects: " + "[" * depth + "]" * depth,
        encoding="utf-8",
    )
    with pytest.raises(ScenarioError):
        load_scenario(deep)


# --- the error-verdict builder must never crash itself ----------------------

def test_error_verdict_survives_a_null_provenance():
    # A caller nulled provenance and also emptied the effects, so evaluation
    # raises ScenarioError and the handler builds an error verdict. That builder
    # used to read provenance.model_dump() and raise AttributeError itself.
    sc = load_scenario(SCENARIOS / "payment-release.yaml")
    broken = sc.model_copy(update={"provenance": None, "effects": []})
    v = evaluate(broken, clean_pair("payment"))
    assert v.status == "error"
    assert v.provenance is None
