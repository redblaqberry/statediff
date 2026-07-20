"""Guards for scenarios that reach the engine without going through the file
parser, and for artifacts that crash the reader instead of failing closed.

A library caller can build ``Scenario``/``Effect`` objects directly, or mutate a
parsed one with ``model_copy``, skipping the validation ``parse_scenario``
performs. The engine re-validates every scenario it evaluates, so a spec a file
could never express cannot evaluate to ``pass`` or escape as a traceback.
"""

from __future__ import annotations

import pytest

from statediff.adapter import ArtifactError, load_pair
from statediff.adapter import _read_json
from statediff.engine import evaluate
from statediff.scenario import load_scenario
from tests.conftest import FIXTURES

REPO_SCENARIOS = FIXTURES.parent / "scenarios"
PAYMENT = FIXTURES / "baseline" / "payment"


def _scenario():
    return load_scenario(REPO_SCENARIOS / "payment-release.yaml")


def _pair():
    return load_pair(PAYMENT / "before-snapshot.json", PAYMENT / "after-snapshot.json")


# --- H1: a scenario that asserts nothing that can fail ---------------------

def test_allowed_only_scenario_evaluates_to_error():
    scenario = _scenario()
    # Move every effect to `allowed`, which justifies coverage but never fails.
    # Against any pair this asserts no task completion, so it must not pass.
    allowed = [e.model_copy(update={"list_name": "allowed"}) for e in scenario.effects]
    mutated = scenario.model_copy(update={"effects": allowed})
    verdict = evaluate(mutated, _pair())
    assert verdict.status == "error"


# --- H2: in-memory scenarios must not bypass scenario-level validation -----

def test_reserved_effect_id_built_in_memory_is_error():
    scenario = _scenario()
    first = scenario.effects[0]
    bad = first.model_copy(update={"spec": first.spec.model_copy(update={"id": "verdict"})})
    mutated = scenario.model_copy(update={"effects": [bad, *scenario.effects[1:]]})
    verdict = evaluate(mutated, _pair())
    assert verdict.status == "error"


def test_duplicate_effect_id_built_in_memory_is_error():
    scenario = _scenario()
    mutated = scenario.model_copy(update={"effects": [*scenario.effects, scenario.effects[0]]})
    verdict = evaluate(mutated, _pair())
    assert verdict.status == "error"


def test_forbidden_invariant_rule_built_in_memory_is_error():
    scenario = _scenario()
    invariant = next((e for e in scenario.effects if e.rule in
                      ("unchanged", "idempotent", "compensated", "correlated")), None)
    if invariant is None:
        pytest.skip("flagship scenario declares no invariant effect to move")
    moved = invariant.model_copy(update={"list_name": "forbidden"})
    others = [e for e in scenario.effects if e is not invariant]
    mutated = scenario.model_copy(update={"effects": [moved, *others]})
    verdict = evaluate(mutated, _pair())
    assert verdict.status == "error"


# --- H3: an unexpected evaluator exception becomes an error verdict ---------

def test_mutated_spec_table_is_error_not_a_traceback():
    scenario = _scenario()
    idx, effect = next((i, e) for i, e in enumerate(scenario.effects) if e.rule == "count_delta")
    # `table: []` makes require_table's set-membership raise TypeError (unhashable
    # list), an exception the engine did not catch. It must become an error
    # verdict, not a traceback and exit 1.
    bad = effect.model_copy(update={"spec": effect.spec.model_copy(update={"table": []})})
    effects = list(scenario.effects)
    effects[idx] = bad
    mutated = scenario.model_copy(update={"effects": effects})
    verdict = evaluate(mutated, _pair())  # must not raise
    assert verdict.status == "error"


# --- H4: a deeply nested JSON artifact fails closed ------------------------

def test_deeply_nested_json_is_an_artifact_error(tmp_path):
    deep = tmp_path / "deep.json"
    depth = 20000
    deep.write_text("[" * depth + "]" * depth, encoding="utf-8")
    with pytest.raises(ArtifactError):
        _read_json(deep)
