"""Engine-level behavior: clean passes, the append-only invariant, verdict
shape, and error propagation."""

import json

from statediff.adapter import load_pair
from statediff.engine import evaluate, evaluate_paths
from statediff.evidence import render
from statediff.gate import to_gate_checks
from statediff.scenario import load_scenario
from tests.conftest import FIXTURES, read_json, write_json
from tests.test_rules import check_by_id, rehash

REPO_SCENARIOS = FIXTURES.parent / "scenarios"
PAYMENT = FIXTURES / "baseline" / "payment"
HOLD = FIXTURES / "baseline" / "hold"


def test_flagship_scenario_passes_on_the_real_capture():
    scenario = load_scenario(REPO_SCENARIOS / "payment-release.yaml")
    pair = load_pair(
        PAYMENT / "before-snapshot.json", PAYMENT / "after-snapshot.json",
        PAYMENT / "before-events.jsonl", PAYMENT / "after-events.jsonl",
    )
    verdict = evaluate(scenario, pair)
    assert verdict.status == "pass"
    assert verdict.unexplained == []
    assert {check.status for check in verdict.checks} == {"pass"}


def test_hold_scenario_passes_on_the_real_capture():
    scenario = load_scenario(REPO_SCENARIOS / "hold-compensation.yaml")
    pair = load_pair(HOLD / "before-snapshot.json", HOLD / "after-snapshot.json")
    verdict = evaluate(scenario, pair)
    assert verdict.status == "pass"
    assert verdict.unexplained == []


def _renamed_payment(tmp_path):
    """The captured payment release with the payment row renamed and the
    hashes recomputed, so the artifact stays internally consistent and the
    audit event still names the payment it was captured for."""
    raw = read_json(PAYMENT / "after-snapshot.json")
    raw["tables"]["payments"][0]["payment_id"] = "PAY-9999"
    after = tmp_path / "renamed-payment.json"
    write_json(after, rehash(raw))
    return load_pair(PAYMENT / "before-snapshot.json", after)


def test_flagship_fails_when_the_row_and_its_audit_event_are_different_payments(tmp_path):
    # The scenario required a payment row AND a PAYMENT_RELEASED event, but not
    # that they concern the same payment, so an unaudited payment passed the
    # oracle whose whole purpose is to catch one.
    scenario = load_scenario(REPO_SCENARIOS / "payment-release.yaml")
    verdict = evaluate(scenario, _renamed_payment(tmp_path))
    assert verdict.status == "fail"
    assert check_by_id(verdict, "payment-audited").status == "fail"
    kinds = {entry["kind"] for entry in check_by_id(verdict, "payment-audited").evidence}
    assert kinds == {"row_not_correlated", "event_names_missing_row"}
    # The counting and identity checks are intact, which is why they could not
    # see this: only the correlation can.
    assert check_by_id(verdict, "one-payment").status == "pass"
    assert check_by_id(verdict, "unexplained").status == "pass"


def test_the_human_report_cannot_contradict_the_verdict_about_correlation(tmp_path):
    # The report used to re-derive the payment-to-audit join for display only.
    # It printed "NO AUDIT EVENT EXISTS" underneath a PASS verdict. Both now
    # read the same evidence, so the line can only appear under a failing
    # correlation check.
    scenario = load_scenario(REPO_SCENARIOS / "payment-release.yaml")
    clean = evaluate(scenario, load_pair(PAYMENT / "before-snapshot.json", PAYMENT / "after-snapshot.json"))
    clean_report = render(clean)
    assert "VERDICT: PASS" in clean_report
    assert "audited by EVT-0001" in clean_report
    assert "NO AUDIT EVENT EXISTS" not in clean_report

    tampered = evaluate(scenario, _renamed_payment(tmp_path))
    report = render(tampered)
    assert "NO AUDIT EVENT EXISTS" in report
    assert "VERDICT: FAIL" in report


def test_hold_scenario_fails_when_the_compensation_names_a_hold_that_does_not_exist(tmp_path):
    # Both compensation events agree on a hold_id, so the pairing rule is
    # satisfied; the hold they name simply is not in the table.
    raw = read_json(HOLD / "after-snapshot.json")
    for row in raw["tables"]["events"]:
        payload = json.loads(row["payload_json"])
        payload["hold_id"] = "HOLD-FAKE"
        row["payload_json"] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    after = tmp_path / "fake-hold.json"
    write_json(after, rehash(raw))

    scenario = load_scenario(REPO_SCENARIOS / "hold-compensation.yaml")
    verdict = evaluate(scenario, load_pair(HOLD / "before-snapshot.json", after))
    assert verdict.status == "fail"
    assert check_by_id(verdict, "hold-pair-nets-out").status == "pass"
    for check_id in ("placed-hold-exists", "released-hold-exists"):
        check = check_by_id(verdict, check_id)
        assert check.status == "fail"
        assert any(entry["kind"] == "event_names_missing_row" for entry in check.evidence)


def test_rewritten_history_fails_append_only(tmp_path):
    # Evaluate MID -> tampered-after: the MID baseline has one event of real
    # history, so rewriting it is detectable. (Over an empty before history
    # any suffix is a valid extension, which is why MID exists.)
    raw = read_json(HOLD / "after-snapshot.json")
    raw["tables"]["events"][0]["actor"] = "ap_approver"
    tampered = tmp_path / "tampered.json"
    write_json(tampered, rehash(raw))
    scenario = load_scenario(REPO_SCENARIOS / "hold-compensation.yaml")
    pair = load_pair(HOLD / "mid-snapshot.json", tampered)
    verdict = evaluate(scenario, pair)
    assert verdict.status == "fail"
    assert check_by_id(verdict, "append_only").status == "fail"
    assert "rewritten or truncated" in check_by_id(verdict, "append_only").detail


def test_verdict_json_shape():
    scenario = load_scenario(REPO_SCENARIOS / "payment-release.yaml")
    pair = load_pair(PAYMENT / "before-snapshot.json", PAYMENT / "after-snapshot.json")
    verdict = evaluate(scenario, pair)
    parsed = json.loads(verdict.to_json())
    assert parsed["format"] == "statediff.verdict.v1"
    assert parsed["scenario_id"] == "SD-PAY-01"
    assert parsed["provenance"]["requirements"] == ["REQ-007", "REQ-012"]
    assert all("list" in check for check in parsed["checks"])
    assert parsed["artifacts"]["before_state_hash"].startswith("38d60e95")


def test_a_scenario_evaluated_against_the_wrong_world_fails():
    scenario = load_scenario(REPO_SCENARIOS / "payment-release.yaml")
    pair = load_pair(HOLD / "before-snapshot.json", HOLD / "after-snapshot.json")
    # Wrong world for this scenario: the expected payment never happens.
    verdict = evaluate(scenario, pair)
    assert verdict.status == "fail"
    failing = [check.id for check in verdict.checks if check.status == "fail"]
    assert "invoice-paid" in failing and "one-payment" in failing
    # And the hold-run changes are unexplained under the payment scenario.
    assert check_by_id(verdict, "unexplained").status == "fail"


def test_error_verdict_never_reads_as_pass(tmp_path):
    # `error` is the status for everything the engine could not evaluate, and
    # it must fail a downstream gate exactly like `fail`. That is a claim about
    # the PROJECTION, so this evaluates a genuine error verdict and pushes it
    # through `to_gate_checks`: a test that asserted over a `fail` verdict
    # instead would stay green if the projection started reading `error` as
    # anything but blocking, which is the one reading that lets a consumer
    # merge an unevaluated run as green.
    scenario = load_scenario(REPO_SCENARIOS / "payment-release.yaml")
    corrupted = tmp_path / "corrupted.json"
    raw = read_json(PAYMENT / "after-snapshot.json")
    raw["state_hash"] = "0" * 64
    write_json(corrupted, raw)

    verdict = evaluate_paths(scenario, PAYMENT / "before-snapshot.json", corrupted)
    assert verdict.status == "error"
    assert "state_hash mismatch" in verdict.error

    gate = to_gate_checks(verdict)
    # Nothing in the projection reads as passing, including the always-present
    # verdict entry a consumer cannot filter away.
    assert all(check["passed"] is False for check in gate)
    by_name = {check["name"]: check for check in gate}
    assert by_name["statediff:verdict"]["passed"] is False
    assert "error" in by_name["statediff:verdict"]["detail"]
    assert by_name["statediff:artifact"]["passed"] is False


def test_a_copied_scenario_evaluates_exactly_like_the_original():
    # A caller holding a Scenario copies it before mutating or shipping it.
    # Transition endpoint presence used to live in an `object()` sentinel that
    # a deep copy duplicated, so every OMITTED endpoint came back looking
    # supplied and pinned to a value nothing can equal: the copy of a scenario
    # that passes its own capture failed it, on an effect the copy never
    # changed.
    scenario = load_scenario(REPO_SCENARIOS / "payment-release.yaml")
    pair = load_pair(PAYMENT / "before-snapshot.json", PAYMENT / "after-snapshot.json")
    original = evaluate(scenario, pair)
    copied = evaluate(scenario.model_copy(deep=True), pair)
    assert original.status == "pass"
    assert copied.status == "pass"
    assert check_by_id(copied, "approval-stamped").status == "pass"
    assert [(check.id, check.status, check.detail) for check in copied.checks] == [
        (check.id, check.status, check.detail) for check in original.checks
    ]
