"""Per-rule semantics, exercised through the engine over perturbed real
artifacts (perturbations keep hashes and counters consistent, so only the rule
under test is in play)."""

import json

import pytest

from statediff.adapter import compute_hashes, load_pair
from statediff.engine import evaluate
from statediff.scenario import ScenarioError, parse_scenario
from tests.conftest import FIXTURES, read_json, write_json

PAYMENT = FIXTURES / "baseline" / "payment"
HOLD = FIXTURES / "baseline" / "hold"


def scenario_with(effects: dict, rules_config: dict | None = None):
    doc = {
        "scenario": "statediff.scenario.v1",
        "id": "SD-TEST",
        "title": "test scenario",
        "adapter": "silobench",
        "effects": effects,
    }
    if rules_config:
        doc["rules_config"] = rules_config
    return parse_scenario(doc)


def rehash(raw: dict) -> dict:
    raw.update(compute_hashes(raw["meta"], raw["tables"]))
    return raw


def perturbed_pair(tmp_path, base: str, mutate):
    src = FIXTURES / "baseline" / base
    raw = read_json(src / "after-snapshot.json")
    mutate(raw)
    after = tmp_path / "after.json"
    write_json(after, rehash(raw))
    return load_pair(src / "before-snapshot.json", after)


def clean_pair(base: str):
    src = FIXTURES / "baseline" / base
    return load_pair(src / "before-snapshot.json", src / "after-snapshot.json")


def check_by_id(verdict, check_id: str):
    return next(check for check in verdict.checks if check.id == check_id)


# transition ---------------------------------------------------------------

def test_transition_wrong_endpoint_fails():
    scenario = scenario_with({"expected": [
        {"transition": {"id": "t", "table": "invoices", "key": {"invoice_id": "INV-2026-00347"},
                        "field": "status", "from": "approved_for_payment", "to": "rejected"}},
    ]})
    verdict = evaluate(scenario, clean_pair("payment"))
    assert verdict.status == "fail"
    assert check_by_id(verdict, "t").status == "fail"
    assert "not the required transition" in check_by_id(verdict, "t").detail


def test_transition_requires_primary_key_column():
    scenario = scenario_with({"expected": [
        {"transition": {"id": "t", "table": "invoices", "key": {"status": "paid"}, "field": "status"}},
    ]})
    verdict = evaluate(scenario, clean_pair("payment"))
    assert verdict.status == "error"
    assert "primary key" in verdict.error


def test_transition_omitted_endpoints_match_any_change():
    # "t" genuinely OMITS from and to: any change of the field satisfies it.
    scenario = scenario_with({"expected": [
        {"transition": {"id": "t", "table": "approval_requests", "key": {"approval_id": "APR-0001"},
                        "field": "completed_ts"}},
        {"transition": {"id": "s", "table": "invoices", "key": {"invoice_id": "INV-2026-00347"},
                        "field": "status"}},
        {"count_delta": {"id": "p", "table": "payments", "added": 1}},
        {"transition": {"id": "a", "table": "approval_requests", "key": {"approval_id": "APR-0001"},
                        "field": "status"}},
        {"event_exists": {"id": "e", "type": "PAYMENT_RELEASED", "count": 1}},
    ]})
    verdict = evaluate(scenario, clean_pair("payment"))
    assert verdict.status == "pass"


# count_delta ---------------------------------------------------------------

def test_count_bounds_validation():
    with pytest.raises(ScenarioError, match="at least one of min/max"):
        scenario_with({"expected": [{"count_delta": {"id": "c", "table": "payments", "added": {}}}]})
    with pytest.raises(ScenarioError, match="min must be <="):
        scenario_with({"expected": [
            {"count_delta": {"id": "c", "table": "payments", "added": {"min": 3, "max": 1}}},
        ]})
    with pytest.raises(ScenarioError, match="at least one of added/removed"):
        scenario_with({"expected": [{"count_delta": {"id": "c", "table": "payments"}}]})


def test_forbidden_count_delta_fires():
    scenario = scenario_with({
        "expected": [{"count_delta": {"id": "p", "table": "payments", "added": 1}}],
        "forbidden": [{"count_delta": {"id": "no-payments", "table": "payments", "added": {"min": 1}}}],
    })
    verdict = evaluate(scenario, clean_pair("payment"))
    assert check_by_id(verdict, "no-payments").status == "fail"
    assert verdict.status == "fail"


def test_count_delta_reads_its_selector_for_presence_and_still_counts():
    # Every selector in the rule set is read for PRESENCE, so no rule can drift
    # back into the truthiness bug `unchanged` had. What differs is the answer
    # to an empty selection: a bound is an assertion the author wrote down and
    # it is checked either way, so a count over a selector that matched nothing
    # is a measured result rather than an invariant that verified nothing.
    supplied = scenario_with({"expected": [
        {"count_delta": {"id": "c", "table": "payments", "added": 1, "match": {}}},
    ]})
    check = check_by_id(evaluate(supplied, clean_pair("payment")), "c")
    assert check.status == "pass"
    assert "matching {}" in check.detail

    matched_nothing = scenario_with({"expected": [
        {"count_delta": {"id": "c", "table": "payments", "added": 0,
                         "match": {"invoice_id": "INV-0000-00000"}}},
    ]})
    assert check_by_id(evaluate(matched_nothing, clean_pair("payment")), "c").status == "pass"


def test_unsatisfied_count_delta_covers_nothing():
    # The payment row matches, but the bound demands two, so the effect fails
    # AND the added row stays unexplained: conditional coverage.
    scenario = scenario_with({"expected": [
        {"count_delta": {"id": "two-payments", "table": "payments", "added": 2}},
    ]})
    verdict = evaluate(scenario, clean_pair("payment"))
    assert check_by_id(verdict, "two-payments").status == "fail"
    assert any(entry["kind"] == "row_added" and entry["table"] == "payments" for entry in verdict.unexplained)


# unchanged -----------------------------------------------------------------

def test_unchanged_violation_reports_the_atoms():
    scenario = scenario_with({"expected": [
        {"unchanged": {"id": "frozen", "table": "invoices"}},
    ]})
    verdict = evaluate(scenario, clean_pair("payment"))
    frozen = check_by_id(verdict, "frozen")
    assert frozen.status == "fail"
    assert any(entry["column"] == "status" for entry in frozen.evidence)


def test_unchanged_where_subset_passes_when_others_change():
    scenario = scenario_with({"expected": [
        {"unchanged": {"id": "frozen", "table": "invoices", "where": {"invoice_id": "INV-2026-00311"}}},
        {"transition": {"id": "s", "table": "invoices", "key": {"invoice_id": "INV-2026-00347"}, "field": "status"}},
        {"count_delta": {"id": "p", "table": "payments", "added": 1}},
        {"transition": {"id": "a", "table": "approval_requests", "key": {"approval_id": "APR-0001"}, "field": "status"}},
        {"transition": {"id": "ts", "table": "approval_requests", "key": {"approval_id": "APR-0001"}, "field": "completed_ts"}},
        {"event_exists": {"id": "e", "type": "PAYMENT_RELEASED", "count": 1}},
    ]})
    assert evaluate(scenario, clean_pair("payment")).status == "pass"


def test_unchanged_where_selecting_nothing_fails_instead_of_passing_vacuously():
    # A `where` asserts its values exist. Matching nothing means the invariant
    # verified nothing, and a gate consumer reading `passed` cannot tell that
    # from a real pass, so it is a failure with its own evidence kind.
    scenario = scenario_with({"expected": [
        {"unchanged": {"id": "frozen", "table": "invoices", "where": {"invoice_id": "INV-0000-00000"}}},
    ]})
    frozen = check_by_id(evaluate(scenario, clean_pair("payment")), "frozen")
    assert frozen.status == "fail"
    assert "matched no rows" in frozen.detail
    assert [entry["kind"] for entry in frozen.evidence] == ["empty_selection"]


def test_unchanged_without_where_still_passes_over_an_empty_table():
    # payments is empty in the before capture. Freezing a whole table claims
    # nothing about which rows exist, so an empty one is a fact about the
    # world rather than a selector that missed.
    scenario = scenario_with({"expected": [{"unchanged": {"id": "frozen", "table": "payments"}}]})
    assert check_by_id(evaluate(scenario, clean_pair("payment")), "frozen").status == "pass"


def test_unchanged_explicit_empty_where_is_still_a_supplied_selector():
    # An explicit `where: {}` is a value the scenario author supplied, so it
    # takes the same path as any other `where`. Tested for truthiness instead
    # of presence, it fell into the bare whole-table branch and reported
    # "all 0 selected row(s) unchanged" over the empty payments table.
    empty_table = scenario_with({"expected": [
        {"unchanged": {"id": "frozen", "table": "payments", "where": {}}},
    ]})
    frozen = check_by_id(evaluate(empty_table, clean_pair("payment")), "frozen")
    assert frozen.status == "fail"
    assert "matched no rows" in frozen.detail
    assert [entry["kind"] for entry in frozen.evidence] == ["empty_selection"]

    # What it means is unchanged: a conjunction of zero conditions still
    # selects every row, so over a populated table it freezes the whole thing.
    populated = scenario_with({"expected": [
        {"unchanged": {"id": "frozen", "table": "invoices", "where": {}}},
    ]})
    frozen = check_by_id(evaluate(populated, clean_pair("payment")), "frozen")
    assert frozen.status == "fail"
    assert any(entry["column"] == "status" for entry in frozen.evidence)


# event_exists --------------------------------------------------------------

def test_event_exists_unknown_type_is_error():
    scenario = scenario_with({"expected": [
        {"event_exists": {"id": "e", "type": "PAYMENT_EXPLODED", "count": 1}},
    ]})
    verdict = evaluate(scenario, clean_pair("payment"))
    assert verdict.status == "error"
    assert "unknown event type" in verdict.error


def test_event_exists_enforces_acting_identity():
    scenario = scenario_with({"expected": [
        {"event_exists": {"id": "e", "type": "PAYMENT_RELEASED", "count": 1, "actor": "ap_clerk"}},
    ]})
    verdict = evaluate(scenario, clean_pair("payment"))
    assert check_by_id(verdict, "e").status == "fail"


# idempotent ----------------------------------------------------------------

def test_idempotent_catches_duplicated_effect(tmp_path):
    def add_duplicate_payment(raw):
        first = dict(raw["tables"]["payments"][0])
        first["payment_id"] = "PAY-0002"
        raw["tables"]["payments"].append(first)
        for row in raw["tables"]["counters"]:
            if row["name"] == "payment":
                row["value"] = 2

    pair = perturbed_pair(tmp_path, "payment", add_duplicate_payment)
    scenario = scenario_with({"expected": [
        {"idempotent": {"id": "unique", "mode": "unique_effect", "table": "payments", "key_fields": ["invoice_id"]}},
    ]})
    verdict = evaluate(scenario, pair)
    unique = check_by_id(verdict, "unique")
    assert unique.status == "fail"
    assert "duplicated" in unique.detail
    assert unique.evidence[0]["key"] == ["INV-2026-00347"]


def test_idempotent_scope_selecting_nothing_fails_instead_of_passing_vacuously():
    # The same rule `unchanged.where` and `correlated.match` already follow. A
    # supplied scope asserts the rows it names exist, and uniqueness over an
    # empty set holds in every world, so `scope: {invoice_id: NOPE}` reported
    # the idempotency invariant as verified while checking nothing at all.
    scenario = scenario_with({"expected": [
        {"idempotent": {"id": "unique", "mode": "unique_effect", "table": "payments",
                        "key_fields": ["invoice_id"], "scope": {"invoice_id": "NOPE"}}},
    ]})
    unique = check_by_id(evaluate(scenario, clean_pair("payment")), "unique")
    assert unique.status == "fail"
    assert "matched no rows" in unique.detail
    assert [entry["kind"] for entry in unique.evidence] == ["empty_selection"]


def test_idempotent_scope_presence_decides_not_its_contents(tmp_path):
    def drop_holds(raw):
        raw["tables"]["holds"] = []
        for row in raw["tables"]["counters"]:
            if row["name"] == "hold":
                row["value"] = 0

    pair = perturbed_pair(tmp_path, "payment", drop_holds)

    def unique_holds(spec_extra):
        return scenario_with({"expected": [
            {"idempotent": {"id": "unique", "mode": "unique_effect", "table": "holds",
                            "key_fields": ["invoice_id"], **spec_extra}},
        ]})

    # An explicit `scope: {}` is a value the author supplied and takes the
    # supplied path, exactly as `unchanged.where` does. Read for truthiness it
    # would fall into the whole-table branch and pass over the empty table.
    empty_scope = check_by_id(evaluate(unique_holds({"scope": {}}), pair), "unique")
    assert empty_scope.status == "fail"
    assert "matched no rows" in empty_scope.detail

    # Omitting the key claims nothing about which rows exist, so the same empty
    # table stays a pass there: that form is a blanket guard, not an assertion.
    assert check_by_id(evaluate(unique_holds({}), pair), "unique").status == "pass"


# compensated ---------------------------------------------------------------

def test_compensated_net_state_violation_fails():
    scenario = scenario_with({"expected": [
        {"compensated": {"id": "pair", "open_event": {"type": "HOLD_PLACED", "payload_key": "hold_id"},
                         "close_event": {"type": "HOLD_RELEASED", "payload_key": "hold_id"},
                         "net_state": {"table": "holds", "where": {"invoice_id": "INV-2026-00318", "active": 1},
                                       "expect_count": 1}}},
    ]})
    verdict = evaluate(scenario, clean_pair("hold"))
    pair_check = check_by_id(verdict, "pair")
    assert pair_check.status == "fail"
    assert any(entry["kind"] == "net_state" for entry in pair_check.evidence)


def test_compensated_open_without_close_fails():
    scenario = scenario_with({"expected": [
        {"compensated": {"id": "pair", "open_event": {"type": "PAYMENT_RELEASED", "payload_key": "payment_id"},
                         "close_event": {"type": "ACCESS_DENIED", "payload_key": "payment_id"},
                         "net_state": {"table": "payments", "where": {"invoice_id": "none"}, "expect_count": 0}}},
    ]})
    verdict = evaluate(scenario, clean_pair("payment"))
    pair_check = check_by_id(verdict, "pair")
    assert pair_check.status == "fail"
    assert any(entry["kind"] == "open_never_closed" for entry in pair_check.evidence)


# correlated ----------------------------------------------------------------

def _audited(table: str, match: dict, event_type: str, payload_key: str):
    return scenario_with({"expected": [
        {"correlated": {"id": "audited", "table": table, "match": match,
                        "event": {"type": event_type, "payload_key": payload_key}}},
    ]})


def test_correlated_ties_a_row_to_the_event_that_names_it():
    scenario = _audited("payments", {"invoice_id": "INV-2026-00347"}, "PAYMENT_RELEASED", "payment_id")
    audited = check_by_id(evaluate(scenario, clean_pair("payment")), "audited")
    assert audited.status == "pass"
    assert [entry["kind"] for entry in audited.evidence] == ["correlated"]
    assert audited.evidence[0]["pk"] == "PAY-0001"
    assert audited.evidence[0]["event_ids"] == ["EVT-0001"]


def test_correlated_catches_a_row_and_an_event_that_are_different_entities(tmp_path):
    # Rename the payment row and leave the audit event naming the one it was
    # captured for. Every count and every event filter still holds: what is
    # wrong is that they no longer concern the same payment, which is exactly
    # what nothing else in the rule set can see.
    def rename_payment(raw):
        raw["tables"]["payments"][0]["payment_id"] = "PAY-9999"

    scenario = _audited("payments", {"invoice_id": "INV-2026-00347"}, "PAYMENT_RELEASED", "payment_id")
    pair = perturbed_pair(tmp_path, "payment", rename_payment)
    audited = check_by_id(evaluate(scenario, pair), "audited")
    assert audited.status == "fail"
    by_kind = {entry["kind"]: entry for entry in audited.evidence}
    assert by_kind["row_not_correlated"]["pk"] == "PAY-9999"
    assert by_kind["row_not_correlated"]["event_ids"] == []
    assert by_kind["event_names_missing_row"]["value"] == "PAY-0001"


def test_correlated_selector_matching_nothing_fails_instead_of_passing_vacuously():
    # With no rows the per-row count clause runs zero times, so the rule
    # asserted nothing. There is no blanket-guard reading of a correspondence.
    scenario = _audited("payments", {"invoice_id": "INV-0000-00000"}, "PAYMENT_RELEASED", "payment_id")
    audited = check_by_id(evaluate(scenario, clean_pair("payment")), "audited")
    assert audited.status == "fail"
    assert [entry["kind"] for entry in audited.evidence] == ["empty_selection"]


def test_correlated_null_payload_value_is_a_named_violation():
    # The captured HOLD_RELEASED payload carries note: null. A null identifies
    # no row, so it is a named violation rather than a silent non-match.
    scenario = _audited("holds", {"hold_id": "HOLD-0003"}, "HOLD_RELEASED", "note")
    audited = check_by_id(evaluate(scenario, clean_pair("hold")), "audited")
    assert audited.status == "fail"
    assert any(entry["kind"] == "event_unusable_key" for entry in audited.evidence)


def test_correlated_is_expected_only():
    with pytest.raises(ScenarioError, match="only valid under expected"):
        scenario_with({"allowed": [
            {"correlated": {"id": "c", "table": "payments",
                            "event": {"type": "PAYMENT_RELEASED", "payload_key": "payment_id"}}},
        ]})


# placement, ids, selectors -------------------------------------------------

def test_invariant_rules_rejected_outside_expected():
    with pytest.raises(ScenarioError, match="only valid under expected"):
        scenario_with({"forbidden": [
            {"unchanged": {"id": "u", "table": "invoices"}},
        ]})


def test_duplicate_and_reserved_ids_rejected():
    with pytest.raises(ScenarioError, match="duplicate effect id"):
        scenario_with({"expected": [
            {"count_delta": {"id": "x", "table": "payments", "added": 1}},
            {"count_delta": {"id": "x", "table": "holds", "added": 1}},
        ]})
    with pytest.raises(ScenarioError, match="reserved"):
        scenario_with({"expected": [
            {"count_delta": {"id": "append_only", "table": "payments", "added": 1}},
        ]})


def test_unknown_rule_and_unknown_table_fail_closed():
    with pytest.raises(ScenarioError, match="unknown rule"):
        scenario_with({"expected": [{"teleport": {"id": "t"}}]})
    scenario = scenario_with({"expected": [
        {"count_delta": {"id": "c", "table": "shadow_ledger", "added": 1}},
    ]})
    verdict = evaluate(scenario, clean_pair("payment"))
    assert verdict.status == "error"
    assert "unknown table" in verdict.error


def test_a_scenario_built_in_python_fails_closed_like_a_parsed_one():
    # `parse_scenario` refuses an unknown rule name, so a file-loaded scenario
    # never reaches dispatch malformed. A library caller can build `Effect`
    # directly, and there the dispatch lookup raised KeyError from OUTSIDE
    # evaluate()'s handler: the exported API answered with a traceback where
    # every other unusable scenario answers with an `error` verdict, so an
    # integration that catches nothing crashed instead of failing the run.
    from statediff.scenario import CountDeltaSpec, Effect, Scenario

    def built(rule: str) -> Scenario:
        return Scenario(
            scenario="statediff.scenario.v1", id="SD-TEST", title="built in python",
            adapter="silobench",
            effects=[Effect(rule=rule, list_name="expected",
                            spec=CountDeltaSpec(id="c", table="payments", added=1))],
        )

    unknown = evaluate(built("teleport"), clean_pair("payment"))
    assert unknown.status == "error"
    assert "unknown rule 'teleport'" in unknown.error

    # A spec belonging to a different rule is the same mistake one step later:
    # it used to surface as an AttributeError from inside the evaluator.
    mismatched = evaluate(built("unchanged"), clean_pair("payment"))
    assert mismatched.status == "error"
    assert "not the UnchangedSpec" in mismatched.error


# profile changes -----------------------------------------------------------

def _flip_docs_outage(tmp_path):
    raw = read_json(PAYMENT / "before-snapshot.json")
    raw["meta"]["docs_outage"] = True
    for row in raw["tables"]["environment"]:
        if row["key"] == "docs_outage":
            row["value"] = "1"
    after = tmp_path / "outage.json"
    write_json(after, rehash(raw))
    return load_pair(PAYMENT / "before-snapshot.json", after)


NOOP_EFFECT = {"count_delta": {"id": "noop", "table": "payments", "added": 0}}


def test_profile_change_is_unexplained_by_default(tmp_path):
    verdict = evaluate(scenario_with({"expected": [NOOP_EFFECT]}), _flip_docs_outage(tmp_path))
    assert verdict.status == "fail"
    assert any(entry.get("table") == "environment" for entry in verdict.unexplained)


def test_profile_change_can_be_allowed(tmp_path):
    scenario = scenario_with({"expected": [NOOP_EFFECT]}, rules_config={"profile_change": "allow"})
    assert evaluate(scenario, _flip_docs_outage(tmp_path)).status == "pass"


# type strictness, raw history, ordering -------------------------------------

def test_boolean_cell_is_rejected_at_load(tmp_path):
    raw = read_json(PAYMENT / "before-snapshot.json")
    raw["tables"]["documents"][0]["version"] = True
    path = tmp_path / "bool-cell.json"
    write_json(path, rehash(raw))
    from statediff.adapter import ArtifactError, load_snapshot
    with pytest.raises(ArtifactError, match="non-SQLite value"):
        load_snapshot(path)


def test_rewritten_payload_json_formatting_fails_append_only(tmp_path):
    # Reformatting a historical payload_json leaves the PARSED payload equal
    # but changes the raw audit row; the prefix invariant must catch it.
    raw = read_json(HOLD / "after-snapshot.json")
    first = raw["tables"]["events"][0]
    assert ": " not in first["payload_json"]
    first["payload_json"] = first["payload_json"].replace(":", ": ", 1)
    path = tmp_path / "reformatted.json"
    write_json(path, rehash(raw))
    from statediff.adapter import load_pair
    pair = load_pair(HOLD / "mid-snapshot.json", path)
    scenario = scenario_with({"expected": [NOOP_EFFECT]})
    verdict = evaluate(scenario, pair)
    assert check_by_id(verdict, "append_only").status == "fail"


def test_negative_event_mutation_seq_is_rejected(tmp_path):
    raw = read_json(PAYMENT / "after-snapshot.json")
    raw["tables"]["events"][0]["mutation_seq"] = -1
    path = tmp_path / "neg-seq.json"
    write_json(path, rehash(raw))
    from statediff.adapter import ArtifactError, load_snapshot
    with pytest.raises(ArtifactError, match="malformed event row"):
        load_snapshot(path)


def test_out_of_order_event_log_is_rejected(tmp_path):
    raw = read_json(HOLD / "after-snapshot.json")
    raw["tables"]["events"].reverse()
    path = tmp_path / "reordered.json"
    write_json(path, rehash(raw))
    from statediff.adapter import ArtifactError, load_snapshot
    with pytest.raises(ArtifactError, match="ordering contract"):
        load_snapshot(path)


def test_string_typed_meta_is_rejected(tmp_path):
    raw = read_json(PAYMENT / "before-snapshot.json")
    raw["meta"]["seed"] = "4711"
    path = tmp_path / "string-seed.json"
    write_json(path, raw)
    from statediff.adapter import ArtifactError, load_snapshot
    with pytest.raises(ArtifactError, match="not a valid snapshot.v1"):
        load_snapshot(path)


# counting semantics addenda -------------------------------------------------

def test_negative_exact_count_is_rejected():
    with pytest.raises(ScenarioError, match="invalid count_delta"):
        scenario_with({"forbidden": [{"count_delta": {"id": "c", "table": "payments", "added": -1}}]})


def test_removed_rows_are_counted_and_justified(tmp_path):
    def remove_vendor(raw):
        raw["tables"]["erp_vendors"] = [
            row for row in raw["tables"]["erp_vendors"] if row["erp_vendor_id"] != "V-2016"
        ]

    raw = read_json(PAYMENT / "before-snapshot.json")
    remove_vendor(raw)
    path = tmp_path / "removed.json"
    write_json(path, rehash(raw))
    from statediff.adapter import load_pair
    pair = load_pair(PAYMENT / "before-snapshot.json", path)

    covered = scenario_with({"expected": [
        {"count_delta": {"id": "gone", "table": "erp_vendors", "removed": 1,
                         "match": {"erp_vendor_id": "V-2016"}}},
    ]})
    assert evaluate(covered, pair).status == "pass"

    uncovered = scenario_with({"expected": [NOOP_EFFECT]})
    verdict = evaluate(uncovered, pair)
    assert verdict.status == "fail"
    assert any(entry["kind"] == "row_removed" for entry in verdict.unexplained)


def test_allowed_effect_justifies_only_while_satisfied(tmp_path):
    def bump_terms(raw):
        for row in raw["tables"]["erp_vendors"]:
            if row["erp_vendor_id"] == "V-2001":
                row["payment_terms_days"] = row["payment_terms_days"] + 5

    raw = read_json(PAYMENT / "before-snapshot.json")
    bump_terms(raw)
    path = tmp_path / "terms.json"
    write_json(path, rehash(raw))
    from statediff.adapter import load_pair
    pair = load_pair(PAYMENT / "before-snapshot.json", path)

    justified = scenario_with({
        "expected": [NOOP_EFFECT],
        "allowed": [{"transition": {"id": "terms-may-move", "table": "erp_vendors",
                                    "key": {"erp_vendor_id": "V-2001"}, "field": "payment_terms_days"}}],
    })
    assert evaluate(justified, pair).status == "pass"

    # An allowed effect whose own constraint is violated covers nothing.
    violated = scenario_with({
        "expected": [NOOP_EFFECT],
        "allowed": [{"count_delta": {"id": "phantom-add", "table": "erp_vendors", "added": 1}}],
    })
    verdict = evaluate(violated, pair)
    assert verdict.status == "fail"
    assert check_by_id(verdict, "unexplained").status == "fail"


def test_an_allowed_effect_that_did_not_fire_is_not_projected_as_a_gate_check():
    # An allowance is never required, so not firing is not a failure; it also
    # satisfied no predicate, so it is not a pass. The gate interface has no
    # third state, and the consuming gate counts the checks it merges toward
    # how much a run actually asserted, so `passed: true` beside "added 0 (want
    # exactly 1)" hands it a named check that verified nothing, while `false`
    # would fail a run over an effect nothing required. It is left out instead.
    from statediff.gate import to_gate_checks

    scenario = scenario_with({
        "expected": [{"count_delta": {"id": "p", "table": "payments", "added": 1}}],
        "allowed": [{"count_delta": {"id": "maybe-hold", "table": "holds", "added": 1}}],
    })
    verdict = evaluate(scenario, clean_pair("payment"))
    unused = check_by_id(verdict, "maybe-hold")
    assert unused.status == "not_applicable"
    assert "did not fire" in unused.detail

    gate = {check["name"]: check for check in to_gate_checks(verdict)}
    assert "statediff:maybe-hold" not in gate
    assert gate["statediff:p"]["passed"] is True
    # Left out, but never silently: the always-present verdict entry names
    # every allowance that dropped out, so the projection cannot quietly shrink.
    assert "maybe-hold" in gate["statediff:verdict"]["detail"]

    fired = scenario_with({
        "expected": [{"count_delta": {"id": "p", "table": "payments", "added": 1}}],
        "allowed": [{"count_delta": {"id": "maybe-payment", "table": "payments", "added": 1}}],
    })
    fired_verdict = evaluate(fired, clean_pair("payment"))
    assert check_by_id(fired_verdict, "maybe-payment").status == "pass"
    # An allowance that DID hold is a real result and stays in the projection.
    fired_gate = {check["name"]: check for check in to_gate_checks(fired_verdict)}
    assert fired_gate["statediff:maybe-payment"]["passed"] is True
    assert "not projected" not in fired_gate["statediff:verdict"]["detail"]


# rule/table restrictions and compensated addenda ----------------------------

def test_rules_cannot_address_counters_or_events():
    for table in ("counters", "events"):
        scenario = scenario_with({"expected": [
            {"unchanged": {"id": "frozen", "table": table}},
        ]})
        verdict = evaluate(scenario, clean_pair("payment"))
        assert verdict.status == "error"
        assert "not addressable" in verdict.error


def test_compensated_same_event_types_rejected():
    with pytest.raises(ScenarioError, match="must differ"):
        scenario_with({"expected": [
            {"compensated": {"id": "pair", "open_event": {"type": "HOLD_PLACED", "payload_key": "hold_id"},
                             "close_event": {"type": "HOLD_PLACED", "payload_key": "hold_id"},
                             "net_state": {"table": "holds", "where": {"active": 1}, "expect_count": 0}}},
        ]})


def test_duplicate_yaml_keys_are_rejected(tmp_path):
    scenario_file = tmp_path / "dup.yaml"
    scenario_file.write_text(
        "scenario: statediff.scenario.v1\n"
        "id: SD-DUP\n"
        "title: dup\n"
        "adapter: silobench\n"
        "effects:\n"
        "  forbidden:\n"
        "    - count_delta: {id: a, table: payments, added: {min: 1}}\n"
        "  forbidden:\n"
        "    - count_delta: {id: b, table: holds, added: {min: 1}}\n",
        encoding="utf-8",
    )
    from statediff.scenario import load_scenario
    with pytest.raises(ScenarioError, match="duplicate mapping key"):
        load_scenario(scenario_file)


def _date_selector_scenario(tmp_path, name: str, literal: str):
    """Two scenarios that differ only in whether the date literal is quoted."""
    path = tmp_path / f"{name}.yaml"
    path.write_text(
        "scenario: statediff.scenario.v1\n"
        f"id: SD-{name.upper()}\n"
        "title: date selector\n"
        "adapter: silobench\n"
        "effects:\n"
        "  expected:\n"
        "    - unchanged:\n"
        "        id: frozen\n"
        "        table: invoices\n"
        f"        where: {{received_date: {literal}}}\n",
        encoding="utf-8",
    )
    from statediff.scenario import load_scenario
    return load_scenario(path)


def test_unquoted_date_selector_behaves_exactly_like_a_quoted_one(tmp_path):
    # INV-2026-00347 was received on 2026-01-03 and its status changes in this
    # capture, so the invariant must FAIL. YAML's implicit timestamp resolver
    # used to decode the unquoted literal into a datetime.date, which matched
    # none of the string cells and reported a pass over the empty selection.
    unquoted = _date_selector_scenario(tmp_path, "unquoted", "2026-01-03")
    quoted = _date_selector_scenario(tmp_path, "quoted", "'2026-01-03'")
    assert unquoted.effects[0].spec.where == {"received_date": "2026-01-03"}
    assert unquoted.effects[0].spec.where == quoted.effects[0].spec.where

    pair = clean_pair("payment")
    unquoted_check = check_by_id(evaluate(unquoted, pair), "frozen")
    quoted_check = check_by_id(evaluate(quoted, pair), "frozen")
    assert unquoted_check.status == "fail"
    assert (unquoted_check.status, unquoted_check.detail) == (quoted_check.status, quoted_check.detail)


def test_scenario_loader_does_not_disarm_timestamps_for_other_yaml_users():
    import datetime

    import yaml

    from statediff.scenario import _StrictLoader
    assert yaml.load("d: 2026-01-03\nt: 2026-01-03T10:00:00Z\n", Loader=_StrictLoader) == {
        "d": "2026-01-03", "t": "2026-01-03T10:00:00Z",
    }
    # Only this loader changed: the resolver table was copied, not edited in
    # place, so every other yaml user in the process still gets dates.
    assert yaml.load("d: 2026-01-03\n", Loader=yaml.SafeLoader) == {"d": datetime.date(2026, 1, 3)}
    # And no other implicit resolver was lost with it.
    assert yaml.load("n: 12\nb: true\nx: null\n", Loader=_StrictLoader) == {"n": 12, "b": True, "x": None}


def test_selectors_are_boolean_strict(tmp_path):
    # The discriminating case is an integer 1 against a pin of `true`: Python
    # reads those as equal, so a strict comparison is the only thing separating
    # them. Event payloads are JSON, which is where a boolean is a legal value
    # to write and the comparison still has to decide it; a table selector
    # cannot even be spelled with one any more, because a cell holds 1 or 0.
    # The earlier version of this test pinned `true` against a cell holding 0,
    # where ordinary `==` returns exactly what a strict compare does, so it
    # asserted nothing about strictness at all.
    def flag_the_payload(raw):
        row = raw["tables"]["events"][0]
        payload = json.loads(row["payload_json"])
        payload["retried"] = 1
        row["payload_json"] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    pair = perturbed_pair(tmp_path, "payment", flag_the_payload)

    def pinned(value):
        return scenario_with({"expected": [
            {"event_exists": {"id": "e", "type": "PAYMENT_RELEASED", "count": 1,
                              "payload_includes": {"retried": value}}},
        ]})

    assert check_by_id(evaluate(pinned(True), pair), "e").status == "fail"
    # The same pin as the integer the payload actually carries does match, so
    # what failed above is the type and not a missing key.
    assert check_by_id(evaluate(pinned(1), pair), "e").status == "pass"


def test_a_selector_value_no_cell_can_hold_is_a_scenario_error():
    # The demonstrated failure: the MID capture ADDS a hold row whose `active`
    # is 1, and a forbidden effect selecting `active: true` counted zero of
    # them, reported pass, and let the whole verdict come back pass. Nothing a
    # snapshot cell can hold equals a YAML boolean, and a value that can never
    # match is refused rather than compared: in an expected effect it fails for
    # a reason the detail cannot explain, and in a forbidden one it disarms the
    # check while reporting success.
    def forbidden_on(value):
        return {"forbidden": [
            {"count_delta": {"id": "no-active-hold", "table": "holds", "added": {"min": 1},
                             "match": {"active": value}}},
        ]}

    for value in (True, False, 1.0, float("nan"), float("inf"), [1], {"a": 1}):
        with pytest.raises(ScenarioError, match="can never match a row"):
            scenario_with(forbidden_on(value))
    # Text, an integer, and null are exactly what a cell holds, so all three
    # stay legal and the check keeps working for the values that can match.
    for value in ("1", 1, None):
        scenario_with(forbidden_on(value))


def test_every_table_selector_is_validated_the_same_way():
    # One shared refusal rather than one per rule: `match`, `where`, `scope`,
    # `net_state.where`, and a transition's key and endpoints all end up
    # compared against a cell, so all of them reject the same values.
    for effects in (
        {"expected": [{"unchanged": {"id": "u", "table": "holds", "where": {"active": True}}}]},
        {"expected": [{"idempotent": {"id": "i", "mode": "unique_effect", "table": "holds",
                                      "key_fields": ["invoice_id"], "scope": {"active": True}}}]},
        {"expected": [{"correlated": {"id": "c", "table": "holds", "match": {"active": True},
                                      "event": {"type": "HOLD_PLACED", "payload_key": "hold_id"}}}]},
        {"expected": [{"compensated": {
            "id": "p",
            "open_event": {"type": "HOLD_PLACED", "payload_key": "hold_id"},
            "close_event": {"type": "HOLD_RELEASED", "payload_key": "hold_id"},
            "net_state": {"table": "holds", "where": {"active": True}, "expect_count": 0}}}]},
        {"expected": [{"transition": {"id": "t", "table": "holds", "key": {"hold_id": True},
                                      "field": "active"}}]},
        {"expected": [{"transition": {"id": "t", "table": "holds", "key": {"hold_id": "HOLD-0003"},
                                      "field": "active", "to": True}}]},
        {"forbidden": [{"count_delta": {"id": "c", "table": "holds", "added": 1,
                                        "match": {"active": True}}}]},
    ):
        with pytest.raises(ScenarioError, match="can never match a row"):
            scenario_with(effects)


def test_a_selector_built_in_python_is_refused_when_it_is_used():
    # `model_construct` skips validation exactly as a caller assembling a spec
    # by hand does, so the runtime selector is the last place that can refuse.
    # Without it the forbidden effect goes back to counting zero of the added
    # `active` = 1 rows and reporting pass.
    from statediff.scenario import Bounds, CountDeltaSpec, Effect, Scenario

    spec = CountDeltaSpec.model_construct(
        id="no-active-hold", table="holds", added=Bounds(min=1), removed=None,
        match={"active": True},
    )
    scenario = Scenario(
        scenario="statediff.scenario.v1", id="SD-TEST", title="built in python",
        adapter="silobench",
        effects=[Effect(rule="count_delta", list_name="forbidden", spec=spec)],
    )
    pair = load_pair(HOLD / "before-snapshot.json", HOLD / "mid-snapshot.json")
    verdict = evaluate(scenario, pair)
    assert verdict.status == "error"
    assert "can never match a row" in verdict.error


def test_payload_pins_reject_values_no_payload_can_carry():
    # Payloads are arbitrary JSON, so a boolean or a float pin is legitimate
    # here and stays legitimate. What cannot appear is what is not JSON: the
    # loader refuses NaN and the infinities, and NaN does not equal even
    # itself, so pinning one reads as a constraint and matches nothing ever.
    scenario_with({"expected": [
        {"event_exists": {"id": "e", "type": "PAYMENT_RELEASED", "count": 1,
                          "payload_includes": {"ok": True, "rate": 1.5, "note": None}}},
    ]})
    for value in (float("nan"), float("inf"), {"nested": float("-inf")}, [float("nan")]):
        with pytest.raises(ScenarioError, match="not a JSON value"):
            scenario_with({"expected": [
                {"event_exists": {"id": "e", "type": "PAYMENT_RELEASED", "count": 1,
                                  "payload_includes": {"x": value}}},
            ]})


def test_a_lone_surrogate_is_refused_wherever_a_selector_or_a_pin_carries_one():
    # Artifacts are UTF-8 files and UTF-8 cannot encode a lone UTF-16 surrogate
    # at all, so no string in an artifact that LOADS holds one. A selector or a
    # pin carrying one is therefore the same defect the boolean and float cases
    # already closed, one domain narrower: it is not a value the run failed to
    # produce, it is a value nothing can produce, and a forbidden effect built
    # on it reports PASS while incapable of firing.
    surrogate = "\ud800"
    for effects in (
        # a table selector value, a transition key, and a transition endpoint
        {"forbidden": [{"count_delta": {"id": "c", "table": "payments", "added": {"min": 1},
                                        "match": {"released_by": surrogate}}}]},
        {"expected": [{"transition": {"id": "t", "table": "invoices",
                                      "key": {"invoice_id": surrogate}, "field": "status"}}]},
        {"expected": [{"transition": {"id": "t", "table": "invoices",
                                      "key": {"invoice_id": "INV-2026-00347"},
                                      "field": "status", "to": surrogate}}]},
        {"expected": [{"unchanged": {"id": "u", "table": "payments",
                                     "where": {"released_by": surrogate}}}]},
        # a payload pin's value, and its KEY: `event_matches` tests the key for
        # presence before it compares anything, so a key no payload can carry
        # answers no for every event there is
        {"forbidden": [{"event_exists": {"id": "e", "type": "PAYMENT_RELEASED", "count": {"min": 1},
                                         "payload_includes": {"payment_id": surrogate}}}]},
        {"forbidden": [{"event_exists": {"id": "e", "type": "PAYMENT_RELEASED", "count": {"min": 1},
                                         "payload_includes": {surrogate: "PAY-0001"}}}]},
        {"forbidden": [{"event_exists": {"id": "e", "type": "PAYMENT_RELEASED", "count": {"min": 1},
                                         "payload_includes": {"nested": {surrogate: 1}}}}]},
        # an event identity filter, which unlike `type` has no registry of known
        # values to be checked against
        {"forbidden": [{"event_exists": {"id": "e", "type": "PAYMENT_RELEASED",
                                         "count": {"min": 1}, "actor": surrogate}}]},
    ):
        with pytest.raises(ScenarioError, match="lone UTF-16 surrogate"):
            scenario_with(effects)

    # What is refused is the unencodable half of a surrogate pair, not non-ASCII
    # text: an astral character is one code point, encodes to UTF-8, and can sit
    # in a cell or a payload like any other string.
    scenario_with({"forbidden": [
        {"count_delta": {"id": "c", "table": "payments", "added": {"min": 1},
                         "match": {"released_by": "\U0001f600"}}},
        {"event_exists": {"id": "e", "type": "PAYMENT_RELEASED", "count": {"min": 1},
                          "actor": "\U0001f600", "payload_includes": {"note": "\U0001f600"}}},
    ]})


def test_a_surrogate_pin_cannot_disarm_a_forbidden_check():
    # The demonstrated failure, from the working check backwards. Pinned to the
    # value the capture really carries the forbidden effect fires and fails;
    # pinned to a lone surrogate it counted zero events and reported PASS, and
    # the scenario passed with a policy check that could not have fired.
    # `event_matches` cannot tell that apart from a real miss, so the refusal
    # has to come before the comparison, for a spec mutated in Python as much
    # as for a parsed one.
    scenario = scenario_with({"forbidden": [
        {"event_exists": {"id": "no-release", "type": "PAYMENT_RELEASED", "count": {"min": 1},
                          "payload_includes": {"payment_id": "PAY-0001"}}},
    ]})
    assert check_by_id(evaluate(scenario, clean_pair("payment")), "no-release").status == "fail"

    scenario.effects[0].spec.payload_includes = {"payment_id": "\ud800"}
    verdict = evaluate(scenario, clean_pair("payment"))
    assert verdict.status == "error"
    assert "lone UTF-16 surrogate" in verdict.error

    # The identity filters are the same hole with no payload involved at all,
    # in two ways: a surrogate they cannot hold, and a non-string type they can
    # never equal. Both are typed `str | None` by the model and both bypasses
    # skip that, and unlike `type` these four have no registry of known values
    # to fall back on.
    scenario.effects[0].spec.payload_includes = None
    scenario.effects[0].spec.actor = "\ud800"
    verdict = evaluate(scenario, clean_pair("payment"))
    assert verdict.status == "error"
    assert "lone UTF-16 surrogate" in verdict.error

    scenario.effects[0].spec.actor = 123
    verdict = evaluate(scenario, clean_pair("payment"))
    assert verdict.status == "error"
    assert "can never match an event" in verdict.error


def test_nan_payload_is_rejected(tmp_path):
    raw = read_json(PAYMENT / "after-snapshot.json")
    raw["tables"]["events"][0]["payload_json"] = '{"payment_id":"PAY-0001","amount":NaN}'
    path = tmp_path / "nan.json"
    write_json(path, rehash(raw))
    from statediff.adapter import ArtifactError, load_snapshot
    with pytest.raises(ArtifactError, match="JSON"):
        load_snapshot(path)


def test_compensated_does_not_pair_events_whose_key_types_differ(tmp_path):
    # The opener carries the integer 1 and the closer the float 1.0. Both
    # artifacts load: a payload is arbitrary JSON, so a float is legal there.
    # Python calls the two values equal and hashes them alike, so the FIFO
    # queue paired them and a fully passing verdict certified a compensation
    # between two events that do not agree on what was opened and closed.
    # `strict_equal` was introduced so numeric types cannot reconcile silently;
    # a rule that pairs by dictionary lookup has to obey it too.
    def retype_hold_ids(raw):
        for row in raw["tables"]["events"]:
            payload = json.loads(row["payload_json"])
            payload["hold_id"] = 1 if row["type"] == "HOLD_PLACED" else 1.0
            row["payload_json"] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    pair = perturbed_pair(tmp_path, "hold", retype_hold_ids)
    scenario = scenario_with({"expected": [
        {"compensated": {"id": "pair", "open_event": {"type": "HOLD_PLACED", "payload_key": "hold_id"},
                         "close_event": {"type": "HOLD_RELEASED", "payload_key": "hold_id"},
                         "net_state": {"table": "holds", "where": {"invoice_id": "INV-2026-00318", "active": 1},
                                       "expect_count": 0}}},
    ]})
    pair_check = check_by_id(evaluate(scenario, pair), "pair")
    assert pair_check.status == "fail"
    # Neither end resolved to the other, and the report says so from both
    # sides rather than reporting a net-state result over a phantom pairing.
    assert {entry["kind"] for entry in pair_check.evidence} == {"open_never_closed", "close_without_open"}

    # Same values, same types on both ends: the pairing still works, so what
    # failed above is the type flip and not the rewritten payload.
    def same_hold_ids(raw):
        for row in raw["tables"]["events"]:
            payload = json.loads(row["payload_json"])
            payload["hold_id"] = 1
            row["payload_json"] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    matched = check_by_id(evaluate(scenario, perturbed_pair(tmp_path, "hold", same_hold_ids)), "pair")
    assert matched.status == "pass"


def test_a_selector_is_refused_even_when_no_row_or_event_reaches_the_comparison(tmp_path):
    # The runtime refusal in `row_matches` only ever sees the values a row
    # actually reaches it with, so the shape of the DATA decided whether a spec
    # built or mutated in Python was validated at all. Every case below is one
    # where no row reaches it: a table nothing touched, a table with no rows on
    # the side the rule reads, or a filter that never looks at a row. The
    # comparison never ran, the effect could not fire, and a FORBIDDEN effect
    # reported the pass its own uncomparable value made inevitable while an
    # invariant blamed the world for a selector that could never have matched.
    def empty_holds(raw):
        raw["tables"]["holds"] = []
        for row in raw["tables"]["counters"]:
            if row["name"] == "hold":
                row["value"] = 0

    clean = clean_pair("payment")
    # holds is empty in the AFTER snapshot here, which is the side `idempotent`
    # and `correlated` read; payments is empty in the BEFORE snapshot, which is
    # the side `unchanged` reads; documents is untouched, so `count_delta` has
    # no atoms to hand its selector.
    no_holds = perturbed_pair(tmp_path, "payment", empty_holds)

    def mutated(effects: dict, field: str, value):
        scenario = scenario_with(effects)
        setattr(scenario.effects[0].spec, field, value)
        return scenario

    cases = [
        (clean, mutated({"forbidden": [{"count_delta": {"id": "x", "table": "documents", "added": {"min": 1},
                                                        "match": {"version": 1}}}]},
                        "match", {"version": True}), "can never match a row"),
        (clean, mutated({"forbidden": [{"transition": {"id": "x", "table": "holds",
                                                       "key": {"hold_id": "HOLD-0003"}, "field": "active"}}]},
                        "key", {"hold_id": True}), "can never match a row"),
        (clean, mutated({"forbidden": [{"transition": {"id": "x", "table": "holds",
                                                       "key": {"hold_id": "HOLD-0003"}, "field": "active"}}]},
                        "to", True), "can never match a row"),
        (clean, mutated({"forbidden": [{"event_exists": {"id": "x", "type": "PAYMENT_RELEASED",
                                                         "count": {"min": 1}, "payload_includes": {"n": 1}}}]},
                        "payload_includes", {"n": float("nan")}), "not a JSON value"),
        (clean, mutated({"expected": [{"unchanged": {"id": "x", "table": "payments",
                                                     "where": {"amount_cents": 1}}}]},
                        "where", {"amount_cents": True}), "can never match a row"),
        (no_holds, mutated({"expected": [{"idempotent": {"id": "x", "mode": "unique_effect", "table": "holds",
                                                         "key_fields": ["invoice_id"], "scope": {"active": 1}}}]},
                           "scope", {"active": True}), "can never match a row"),
        (no_holds, mutated({"expected": [{"correlated": {"id": "x", "table": "holds", "match": {"active": 1},
                                                         "event": {"type": "HOLD_PLACED",
                                                                   "payload_key": "hold_id"}}}]},
                           "match", {"active": True}), "can never match a row"),
    ]
    for pair, scenario, expected in cases:
        rule = scenario.effects[0].rule
        verdict = evaluate(scenario, pair)
        assert verdict.status == "error", rule
        assert expected in verdict.error, rule


def test_a_compensated_net_state_selector_is_refused_over_an_empty_table(tmp_path):
    # `expect_count: 0` is the shipped hold scenario's shape, and it is exactly
    # where an uncomparable `where` hides: nothing selects, zero is what was
    # asserted, and the invariant reports a compensation as netted out while
    # its selector could not have counted a row in any world.
    def drop_holds(raw):
        raw["tables"]["holds"] = []
        for row in raw["tables"]["counters"]:
            if row["name"] == "hold":
                row["value"] = 0

    scenario = scenario_with({"expected": [
        {"compensated": {"id": "pair", "open_event": {"type": "HOLD_PLACED", "payload_key": "hold_id"},
                         "close_event": {"type": "HOLD_RELEASED", "payload_key": "hold_id"},
                         "net_state": {"table": "holds", "where": {"active": 1}, "expect_count": 0}}},
    ]})
    scenario.effects[0].spec.net_state.where = {"active": True}
    verdict = evaluate(scenario, perturbed_pair(tmp_path, "hold", drop_holds))
    assert verdict.status == "error"
    assert "can never match a row" in verdict.error


def test_a_rule_that_asserts_nothing_is_an_error_rather_than_a_pass():
    # count_delta with neither bound measures both counts and then makes no
    # claim about either, so `satisfied` reports only that no bound was broken.
    # The parse-time validator refuses it; a spec mutated in Python skipped it.
    scenario = scenario_with({"expected": [{"count_delta": {"id": "c", "table": "payments", "added": 1}}]})
    scenario.effects[0].spec.added = None
    verdict = evaluate(scenario, clean_pair("payment"))
    assert verdict.status == "error"
    assert "at least one of added/removed" in verdict.error


def test_a_count_no_set_of_matches_can_have_is_refused_at_evaluation():
    # Pydantic validates when a model is BUILT, not when an attribute is
    # assigned, so every bound below is legal to write onto a parsed spec and
    # none of them survives its own model. `count_satisfied` then counts
    # honestly and answers only that the counts disagree, which is
    # indistinguishable from a run that fell short: a FORBIDDEN effect wanting
    # exactly -1 events counted one, could not have counted -1 in any world,
    # and reported PASS. A count that is not a number at all is worse than
    # unreachable, because `count_satisfied` reads `.min` off it and raised
    # AttributeError from outside the handler that turns every other unusable
    # scenario into an `error` verdict.
    from statediff.scenario import Bounds

    def mutated(effects: dict, field: str, value, holder=None):
        scenario = scenario_with(effects)
        target = scenario.effects[0].spec
        setattr(target if holder is None else getattr(target, holder), field, value)
        return scenario

    forbidden_events = {"forbidden": [
        {"event_exists": {"id": "x", "type": "PAYMENT_RELEASED", "count": {"min": 1}}},
    ]}
    cases = [
        (mutated(forbidden_events, "count", -1), "negative size"),
        (mutated(forbidden_events, "count", Bounds.model_construct(min=5, max=1)), "no count can satisfy"),
        (mutated(forbidden_events, "count", Bounds.model_construct(min=None, max=None)), "bounds nothing"),
        (mutated(forbidden_events, "count", Bounds.model_construct(min=0, max=None)), "asserts nothing"),
        (mutated(forbidden_events, "count", Bounds.model_construct(min=-1, max=None)), "negative size"),
        (mutated(forbidden_events, "count", 1.0), "not a whole number"),
        (mutated(forbidden_events, "count", True), "not a whole number"),
        (mutated({"forbidden": [{"count_delta": {"id": "x", "table": "payments", "added": 1}}]},
                 "added", -1), "negative size"),
        (mutated({"forbidden": [{"count_delta": {"id": "x", "table": "payments", "removed": 1}}]},
                 "removed", -1), "negative size"),
        (mutated({"expected": [{"correlated": {"id": "x", "table": "payments",
                                               "event": {"type": "PAYMENT_RELEASED",
                                                         "payload_key": "payment_id"}}}]},
                 "count", -1), "negative size"),
        (mutated({"expected": [{"compensated": {
            "id": "x", "open_event": {"type": "HOLD_PLACED", "payload_key": "hold_id"},
            "close_event": {"type": "HOLD_RELEASED", "payload_key": "hold_id"},
            "net_state": {"table": "holds", "where": {"active": 1}, "expect_count": 0}}}]},
            "expect_count", -1, "net_state"), "negative size"),
    ]
    for scenario, expected in cases:
        rule = scenario.effects[0].rule
        verdict = evaluate(scenario, clean_pair("payment"))
        assert verdict.status == "error", rule
        assert expected in verdict.error, (rule, verdict.error)


def test_an_idempotency_key_of_no_fields_is_refused_rather_than_reported_unique():
    # With no key fields every row groups under the empty tuple, so the rule
    # answers "duplicated" for a table holding two rows and "unique" for one
    # holding at most one. Neither answer is about a key, and the passing one
    # made the whole flagship scenario report pass while handing a consumer an
    # idempotency guarantee over nothing at all. `require_columns` cannot stand
    # in for the model here: zero columns are trivially all known.
    from statediff.scenario import load_scenario

    scenario = load_scenario(FIXTURES.parent / "scenarios" / "payment-release.yaml")
    assert evaluate(scenario, clean_pair("payment")).status == "pass"

    unique = next(effect for effect in scenario.effects if effect.rule == "idempotent")
    unique.spec.key_fields = []
    verdict = evaluate(scenario, clean_pair("payment"))
    assert verdict.status == "error"
    assert "at least one key_field" in verdict.error


def test_a_compensated_effect_pairing_a_type_with_itself_is_an_error(tmp_path):
    # open and close types must differ; the model refuses equal types at
    # construction and a mutated spec skips that. The loop reads a matching
    # event as an opener before it can be a closer, so with no such events
    # present the incoherence hides and a satisfied net-state reports the pass.
    def drop_holds(raw):
        raw["tables"]["holds"] = []
        for row in raw["tables"]["counters"]:
            if row["name"] == "hold":
                row["value"] = 0

    scenario = scenario_with({"expected": [
        {"compensated": {"id": "pair", "open_event": {"type": "HOLD_PLACED", "payload_key": "hold_id"},
                         "close_event": {"type": "HOLD_RELEASED", "payload_key": "hold_id"},
                         "net_state": {"table": "holds", "where": {"active": 1}, "expect_count": 0}}},
    ]})
    scenario.effects[0].spec.close_event.type = "HOLD_PLACED"
    verdict = evaluate(scenario, perturbed_pair(tmp_path, "hold", drop_holds))
    assert verdict.status == "error"
    assert "types must differ" in verdict.error


def test_a_transition_key_that_names_no_single_column_is_an_error():
    # An empty key used to raise StopIteration out of the evaluator, from
    # outside the handler that turns every other unusable scenario into an
    # `error` verdict, so a library caller got a traceback instead of a result.
    scenario = scenario_with({"expected": [
        {"transition": {"id": "t", "table": "holds", "key": {"hold_id": "HOLD-0003"}, "field": "active"}},
    ]})
    scenario.effects[0].spec.key = {}
    verdict = evaluate(scenario, clean_pair("payment"))
    assert verdict.status == "error"
    assert "exactly one column" in verdict.error


def test_a_scenario_with_no_effects_cannot_pass():
    # Only the append-only prefix and the unexplained sweep would run, and an
    # unchanged pair satisfies both, so an empty scenario reported `pass` over
    # a run it asserted nothing about. Refused when the model is built, and
    # again in the engine, because model_copy and model_construct skip
    # validation exactly as a caller assembling one by hand does.
    from pydantic import ValidationError

    from statediff.scenario import Scenario

    with pytest.raises(ValidationError):
        Scenario(scenario="statediff.scenario.v1", id="SD-EMPTY", title="declares nothing",
                 adapter="silobench", effects=[])

    scenario = scenario_with({"expected": [{"count_delta": {"id": "c", "table": "payments", "added": 1}}]})
    verdict = evaluate(scenario.model_copy(update={"effects": []}), clean_pair("payment"))
    assert verdict.status == "error"
    assert "declares no effects" in verdict.error


def test_an_omitted_transition_endpoint_survives_copying_and_serialisation():
    # Presence used to be an `object()` sentinel, which `model_copy(deep=True)`
    # duplicated into a different object: the omitted endpoint then read as
    # supplied and pinned to a value nothing equals, so a deep-copied scenario
    # failed its own clean capture. The same sentinel made the spec
    # unserialisable, so a caller could not store or ship one at all.
    scenario = scenario_with({"expected": [
        {"transition": {"id": "t", "table": "approval_requests", "key": {"approval_id": "APR-0001"},
                        "field": "completed_ts", "from": None}},
    ]})
    spec = scenario.effects[0].spec
    # `from: null` is a REAL endpoint (the timestamp starts out null) and `to`
    # was never written, so the two must not read alike.
    assert (spec.has_from, spec.has_to) == (True, False)
    copied = spec.model_copy(deep=True)
    assert (copied.has_from, copied.has_to) == (True, False)

    from statediff.scenario import TransitionSpec
    # A plain dump must not raise: the sentinel was an ordinary object, which
    # pydantic cannot serialise at all, so a valid spec could not be stored or
    # shipped. It writes both endpoints, so it is the dump that carries no
    # presence and the one below that does.
    assert json.loads(spec.model_dump_json(by_alias=True))["to"] is None
    dumped = json.loads(spec.model_dump_json(by_alias=True, exclude_unset=True))
    assert "to" not in dumped
    restored = TransitionSpec.model_validate(dumped)
    assert (restored.has_from, restored.has_to) == (True, False)


def test_a_null_transition_endpoint_is_a_pin_and_not_an_omission():
    # Presence decides, not the value: `completed_ts` goes null -> timestamp in
    # this capture, so pinning `to: null` must FAIL. Read from the value, an
    # explicit null endpoint is indistinguishable from an omitted one, and an
    # omitted endpoint matches anything, so the pin would silently accept the
    # very transition it was written to forbid.
    pinned_null = scenario_with({"expected": [
        {"transition": {"id": "t", "table": "approval_requests", "key": {"approval_id": "APR-0001"},
                        "field": "completed_ts", "to": None}},
    ]})
    check = check_by_id(evaluate(pinned_null, clean_pair("payment")), "t")
    assert check.status == "fail"
    assert "not the required transition" in check.detail

    # Omitting it is the other reading and still matches any change.
    omitted = scenario_with({"expected": [
        {"transition": {"id": "t", "table": "approval_requests", "key": {"approval_id": "APR-0001"},
                        "field": "completed_ts"}},
    ]})
    assert check_by_id(evaluate(omitted, clean_pair("payment")), "t").status == "pass"


def test_compensated_null_correlation_value_is_a_violation():
    # The captured HOLD_RELEASED payload carries note: null; correlating on it
    # must be a named violation, not a silent miss or a crash.
    scenario = scenario_with({"expected": [
        {"compensated": {"id": "pair", "open_event": {"type": "HOLD_PLACED", "payload_key": "hold_id"},
                         "close_event": {"type": "HOLD_RELEASED", "payload_key": "note"},
                         "net_state": {"table": "holds", "where": {"invoice_id": "INV-2026-00318", "active": 1},
                                       "expect_count": 0}}},
    ]})
    verdict = evaluate(scenario, clean_pair("hold"))
    pair_check = check_by_id(verdict, "pair")
    assert pair_check.status == "fail"
    assert any(entry["kind"] == "close_unusable_key" for entry in pair_check.evidence)


# event selector payload keys -----------------------------------------------

def test_event_selector_payload_key_rejects_a_lone_surrogate():
    # `payload_key` is a JSON object key looked up in an event payload, so a lone
    # surrogate reaches no key any UTF-8 artifact can carry. Unlike the event
    # `type`, it has no registry to be checked against, so nothing else refuses
    # it: a compensated or correlated effect built on one can never correlate.
    surrogate = "\ud800"
    for effects in (
        {"expected": [{"compensated": {"id": "p",
            "open_event": {"type": "HOLD_PLACED", "payload_key": surrogate},
            "close_event": {"type": "HOLD_RELEASED", "payload_key": "hold_id"},
            "net_state": {"table": "holds", "where": {"active": 1}, "expect_count": 0}}}]},
        {"expected": [{"correlated": {"id": "c", "table": "payments",
            "event": {"type": "PAYMENT_RELEASED", "payload_key": surrogate}}}]},
    ):
        with pytest.raises(ScenarioError, match="lone UTF-16 surrogate"):
            scenario_with(effects)
    # An astral character is one code point, encodes to UTF-8, and stays legal.
    scenario_with({"expected": [{"correlated": {"id": "c", "table": "payments",
        "event": {"type": "PAYMENT_RELEASED", "payload_key": "\U0001f600"}}}]})


def test_a_surrogate_payload_key_cannot_disarm_a_compensated_check():
    # The demonstrated failure: with no events of the open/close type in the
    # pair the pairing loop never touches the payload key, so a `compensated`
    # effect pinned to a key no artifact can carry reported "0 ... each closed"
    # as a pass. `correlation_value` cannot tell that apart from a real miss, so
    # the refusal has to come before the loop, for a spec mutated in Python as
    # much as for a parsed one. The payment pair carries no HOLD events at all.
    scenario = scenario_with({"expected": [
        {"compensated": {"id": "pair",
            "open_event": {"type": "HOLD_PLACED", "payload_key": "hold_id"},
            "close_event": {"type": "HOLD_RELEASED", "payload_key": "hold_id"},
            "net_state": {"table": "holds", "where": {"invoice_id": "none"}, "expect_count": 0}}},
    ]})
    scenario.effects[0].spec.open_event.payload_key = "\ud800"
    verdict = evaluate(scenario, clean_pair("payment"))
    assert verdict.status == "error"
    assert "lone UTF-16 surrogate" in verdict.error


def test_an_unknown_event_type_or_table_refusal_is_utf8_encodable():
    # The refusal names the offending value. When that value is a lone surrogate
    # (a type or table mutated to one, or one a scenario file escaped), a bare
    # '{value}' put the unencodable text straight into the message, and the error
    # verdict built from it could not be written to stdout: `check --json` died
    # where it promises a machine-readable refusal. Quoted with !r, the message
    # is ASCII-safe, so the whole verdict round-trips to UTF-8 as the CLI writes.
    surrogate = "\ud800"
    for effects, needle in (
        ({"expected": [{"event_exists": {"id": "e", "type": surrogate, "count": 1}}]},
         "unknown event type"),
        ({"expected": [{"count_delta": {"id": "c", "table": surrogate, "added": 1}}]},
         "unknown table"),
    ):
        verdict = evaluate(scenario_with(effects), clean_pair("payment"))
        assert verdict.status == "error"
        assert needle in verdict.error
        verdict.error.encode("utf-8")
        json.dumps(verdict.model_dump(by_alias=True), ensure_ascii=False).encode("utf-8")


# payload_includes presence, not truthiness ---------------------------------

def test_payload_includes_empty_mapping_is_a_supplied_zero_pin_filter():
    # An explicit `payload_includes: {}` is a real value the author wrote: a
    # conjunction of zero pins matches every event of the type, exactly the way
    # `match: {}` selects every row. It is accepted and applied, not skipped.
    zero = scenario_with({"expected": [
        {"event_exists": {"id": "e", "type": "PAYMENT_RELEASED", "count": 1,
                          "payload_includes": {}}},
    ]})
    assert check_by_id(evaluate(zero, clean_pair("payment")), "e").status == "pass"


def test_a_pin_emptied_in_python_cannot_pass_an_expected_effect_vacuously():
    # The demonstrated failure: an expected event_exists whose pins do NOT match,
    # mutated to `payload_includes=[]`, used to skip both the type validation
    # (`values or {}` coerced the list to `{}`) and the pin loop (`if
    # payload_includes:` read the list as absent), matching the event on its type
    # alone and passing while its detail still claimed the pins were checked.
    scenario = scenario_with({"expected": [
        {"event_exists": {"id": "audit", "type": "PAYMENT_RELEASED", "count": 1,
                          "payload_includes": {"payment_id": "NOT-THE-CAPTURED-ID"}}},
    ]})
    assert check_by_id(evaluate(scenario, clean_pair("payment")), "audit").status == "fail"

    scenario.effects[0].spec.payload_includes = []
    verdict = evaluate(scenario, clean_pair("payment"))
    assert verdict.status == "error"
    assert "never match an event payload" in verdict.error

    # A non-empty non-mapping is the same shape error and must not reach
    # `event_matches`, where `.items()` would raise past the handler.
    scenario.effects[0].spec.payload_includes = [1, 2]
    assert evaluate(scenario, clean_pair("payment")).status == "error"


def test_a_selector_mutated_to_a_non_mapping_is_an_error_not_a_crash():
    # `match`, `where`, `scope`, and `net_state.where` are all read with
    # `.keys()`. A spec mutated in Python to a list rather than a mapping is not
    # empty, it is unusable, and reading `.keys()` off it raised AttributeError
    # from OUTSIDE the handler that turns every other unusable scenario into an
    # `error` verdict. The cell-domain refusal runs before the column check now.
    cases = [
        ({"forbidden": [{"count_delta": {"id": "x", "table": "payments", "added": {"min": 1},
                                         "match": {"released_by": "ap_approver"}}}]}, "match", None),
        ({"expected": [{"unchanged": {"id": "x", "table": "invoices",
                                      "where": {"invoice_id": "INV-2026-00347"}}}]}, "where", None),
        ({"expected": [{"idempotent": {"id": "x", "mode": "unique_effect", "table": "payments",
                                       "key_fields": ["invoice_id"],
                                       "scope": {"invoice_id": "INV-2026-00347"}}}]}, "scope", None),
        ({"expected": [{"correlated": {"id": "x", "table": "payments", "match": {"invoice_id": "X"},
                                       "event": {"type": "PAYMENT_RELEASED", "payload_key": "payment_id"}}}]},
         "match", None),
        ({"expected": [{"compensated": {"id": "x",
            "open_event": {"type": "HOLD_PLACED", "payload_key": "hold_id"},
            "close_event": {"type": "HOLD_RELEASED", "payload_key": "hold_id"},
            "net_state": {"table": "holds", "where": {"active": 1}, "expect_count": 0}}}]},
         "where", "net_state"),
    ]
    for effects, field, holder in cases:
        scenario = scenario_with(effects)
        target = scenario.effects[0].spec
        setattr(target if holder is None else getattr(target, holder), field, [])
        verdict = evaluate(scenario, clean_pair("payment"))
        rule = scenario.effects[0].rule
        assert verdict.status == "error", rule
        assert "can never match a row" in verdict.error, rule


# scenario identity / display strings ---------------------------------------

def test_scenario_identity_and_display_strings_reject_a_lone_surrogate():
    # `id`, `title`, and provenance are written verbatim into the human report
    # and the verdict JSON. Pydantic refuses a lone surrogate in a length-
    # constrained string (an effect id) but not in a plain one, so these slipped
    # through and the report and `check --json` then died with an encoding error
    # instead of the machine-readable error verdict a bad scenario is promised.
    base = {"scenario": "statediff.scenario.v1", "id": "SD", "title": "t",
            "adapter": "silobench",
            "effects": {"expected": [{"count_delta": {"id": "c", "table": "payments", "added": 1}}]}}
    surrogate = "\ud800"
    for field in ("id", "title"):
        with pytest.raises(ScenarioError, match="lone UTF-16 surrogate"):
            parse_scenario({**base, field: surrogate})
    with pytest.raises(ScenarioError, match="lone UTF-16 surrogate"):
        parse_scenario({**base, "provenance": {"requirements": [surrogate]}})
    with pytest.raises(ScenarioError, match="lone UTF-16 surrogate"):
        parse_scenario({**base, "provenance": {"silobench_task": surrogate}})
    # An astral character is one code point, encodes to UTF-8, and stays legal.
    parse_scenario({**base, "id": "\U0001f600", "title": "\U0001f600",
                    "provenance": {"turns": ["\U0001f600"]}})


# recursive / pathological payload aliases ----------------------------------

def test_a_cyclic_payload_alias_is_a_scenario_error_not_a_stack_overflow(tmp_path):
    # A YAML anchor can alias a node back into itself, so `payload_includes: {k:
    # &a [*a]}` builds a list that contains itself. Walking it with no guard
    # recursed until the stack gave out, and a RecursionError is not a
    # ScenarioError, so `check --json` died with a traceback where a bad scenario
    # is promised a machine-readable error verdict.
    scenario_file = tmp_path / "cyclic.yaml"
    scenario_file.write_text(
        "scenario: statediff.scenario.v1\n"
        "id: SD-CYCLE\n"
        "title: cyclic alias\n"
        "adapter: silobench\n"
        "effects:\n"
        "  expected:\n"
        "    - event_exists:\n"
        "        id: e\n"
        "        type: PAYMENT_RELEASED\n"
        "        count: 1\n"
        "        payload_includes:\n"
        "          k: &a\n"
        "            - *a\n",
        encoding="utf-8",
    )
    from statediff.scenario import load_scenario
    # The cycle-specific phrase, so this isolates the ancestor guard from the
    # depth cap (either would stop the stack overflow, but a true cycle is
    # caught at once rather than after MAX_PIN_DEPTH wasted levels).
    with pytest.raises(ScenarioError, match="refers back into a container"):
        load_scenario(scenario_file)


def test_a_pathologically_deep_payload_pin_is_refused_before_the_stack_overflows():
    # Even without a cycle, a payload pin nested past the depth cap is a
    # malformed alias structure rather than a value any event payload can carry,
    # and refusing it keeps a RecursionError from escaping evaluate()'s handler.
    scenario = scenario_with({"expected": [
        {"event_exists": {"id": "e", "type": "PAYMENT_RELEASED", "count": 1}},
    ]})
    deep = current = []
    for _ in range(5000):
        nxt = []
        current.append(nxt)
        current = nxt
    scenario.effects[0].spec.payload_includes = {"k": deep}
    verdict = evaluate(scenario, clean_pair("payment"))
    assert verdict.status == "error"
    assert "nests deeper" in verdict.error


def test_a_shared_payload_alias_is_not_mistaken_for_a_cycle():
    # A YAML anchor aliased by two siblings is a shared reference, not a cycle:
    # `{x: *a, y: *a}` is the finite `{x: [...], y: [...]}`. The cycle guard
    # tracks only ancestors on the current path, so it must stay legal.
    scenario = scenario_with({"expected": [
        {"event_exists": {"id": "e", "type": "PAYMENT_RELEASED", "count": 1}},
    ]})
    shared = ["a", "b"]
    scenario.effects[0].spec.payload_includes = {"x": shared, "y": shared}
    verdict = evaluate(scenario, clean_pair("payment"))
    # Whatever the count outcome, it must NOT be a cyclic-alias scenario error.
    assert not (verdict.status == "error" and "cyclic" in (verdict.error or ""))
