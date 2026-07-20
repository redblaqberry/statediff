"""CLI contract: exit codes 0/1/2, JSON purity on stdout, bundle round-trip,
gate projection, explain rendering."""

import json

from typer.testing import CliRunner

from statediff.cli import app
from tests.conftest import FIXTURES
from tests.test_adapter import with_duplicate_key

runner = CliRunner()

PAYMENT = FIXTURES / "baseline" / "payment"
DEFECTS = FIXTURES / "defects"
SCENARIO = str(FIXTURES.parent / "scenarios" / "payment-release.yaml")


def _capture(bundle, *extra):
    return runner.invoke(app, [
        "capture",
        "--before", str(PAYMENT / "before-snapshot.json"),
        "--after", str(PAYMENT / "after-snapshot.json"),
        *extra,
        "--out", str(bundle),
    ])


def _manifest(bundle):
    return json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))


def test_check_clean_pair_exits_zero():
    result = runner.invoke(app, [
        "check", "--scenario", SCENARIO,
        "--before", str(PAYMENT / "before-snapshot.json"),
        "--after", str(PAYMENT / "after-snapshot.json"),
    ])
    assert result.exit_code == 0
    assert "VERDICT: PASS" in result.output


def test_check_flagship_defect_exits_one_with_correlation():
    result = runner.invoke(app, [
        "check", "--scenario", SCENARIO,
        "--before", str(PAYMENT / "before-snapshot.json"),
        "--after", str(DEFECTS / "duplicate-payment" / "after-snapshot.json"),
    ])
    assert result.exit_code == 1
    assert "VERDICT: FAIL" in result.output
    assert "PAY-0002" in result.output
    assert "NO AUDIT EVENT EXISTS" in result.output


def test_check_broken_artifact_exits_two():
    result = runner.invoke(app, [
        "check", "--scenario", SCENARIO,
        "--before", str(PAYMENT / "before-snapshot.json"),
        "--after", str(DEFECTS / "broken-hash" / "after-snapshot.json"),
    ])
    assert result.exit_code == 2
    assert "VERDICT: ERROR" in result.output


def test_check_json_stdout_is_pure_json():
    result = runner.invoke(app, [
        "check", "--scenario", SCENARIO,
        "--before", str(PAYMENT / "before-snapshot.json"),
        "--after", str(PAYMENT / "after-snapshot.json"),
        "--json",
    ])
    assert result.exit_code == 0
    verdict = json.loads(result.stdout)
    assert verdict["format"] == "statediff.verdict.v1"
    assert verdict["status"] == "pass"
    assert all("list" in check for check in verdict["checks"])


def test_check_gate_projection_includes_verdict_check():
    result = runner.invoke(app, [
        "check", "--scenario", SCENARIO,
        "--before", str(PAYMENT / "before-snapshot.json"),
        "--after", str(DEFECTS / "duplicate-payment" / "after-snapshot.json"),
        "--gate",
    ])
    assert result.exit_code == 1
    checks = json.loads(result.stdout)
    by_name = {check["name"]: check["passed"] for check in checks}
    assert by_name["statediff:verdict"] is False
    assert by_name["statediff:at-most-one-payment-per-invoice"] is False


def test_capture_bundle_roundtrip(tmp_path):
    bundle = tmp_path / "bundle"
    captured = runner.invoke(app, [
        "capture",
        "--before", str(PAYMENT / "before-snapshot.json"),
        "--after", str(PAYMENT / "after-snapshot.json"),
        "--before-events", str(PAYMENT / "before-events.jsonl"),
        "--after-events", str(PAYMENT / "after-events.jsonl"),
        "--out", str(bundle),
    ])
    assert captured.exit_code == 0
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "statediff.bundle.v1"

    checked = runner.invoke(app, ["check", "--scenario", SCENARIO, "--bundle", str(bundle)])
    assert checked.exit_code == 0


def test_capture_rejects_broken_artifacts(tmp_path):
    result = runner.invoke(app, [
        "capture",
        "--before", str(PAYMENT / "before-snapshot.json"),
        "--after", str(DEFECTS / "broken-hash" / "after-snapshot.json"),
        "--out", str(tmp_path / "bundle"),
    ])
    assert result.exit_code == 2


def test_explain_renders_a_stored_verdict(tmp_path):
    produced = runner.invoke(app, [
        "check", "--scenario", SCENARIO,
        "--before", str(PAYMENT / "before-snapshot.json"),
        "--after", str(DEFECTS / "missing-audit-event" / "after-snapshot.json"),
        "--json",
    ])
    assert produced.exit_code == 1
    verdict_file = tmp_path / "verdict.json"
    verdict_file.write_text(produced.stdout, encoding="utf-8")
    explained = runner.invoke(app, ["explain", str(verdict_file)])
    assert explained.exit_code == 0
    assert "audit-payment" in explained.output
    assert "VERDICT: FAIL" in explained.output


def test_version_flag():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "statediff 0.1.0" in result.output


def test_invalid_scenario_with_json_still_emits_machine_readable_error(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("scenario: statediff.scenario.v1\nid: X\n", encoding="utf-8")
    result = runner.invoke(app, [
        "check", "--scenario", str(bad),
        "--before", str(PAYMENT / "before-snapshot.json"),
        "--after", str(PAYMENT / "after-snapshot.json"),
        "--json",
    ])
    assert result.exit_code == 2
    verdict = json.loads(result.stdout)
    assert verdict["status"] == "error"
    assert verdict["format"] == "statediff.verdict.v1"


def test_unknown_effect_lists_of_mixed_types_still_produce_a_verdict(tmp_path):
    # `effects` naming both an integer key and a string key is a scenario
    # error, and it has to arrive as one. Reporting the unknown names through a
    # plain `sorted` raised TypeError from outside every handler that turns a
    # bad scenario into an error verdict, so --json died with a traceback and a
    # consumer got no machine-readable output and an exit code that reads as an
    # ordinary failing run rather than an unevaluated one.
    bad = tmp_path / "mixed-lists.yaml"
    bad.write_text(
        "scenario: statediff.scenario.v1\n"
        "id: SD-MIXED\n"
        "title: mixed unknown effect lists\n"
        "adapter: silobench\n"
        "effects:\n"
        "  1: []\n"
        "  bogus: []\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, [
        "check", "--scenario", str(bad),
        "--before", str(PAYMENT / "before-snapshot.json"),
        "--after", str(PAYMENT / "after-snapshot.json"),
        "--json",
    ])
    assert result.exit_code == 2
    verdict = json.loads(result.stdout)
    assert verdict["status"] == "error"
    assert "unknown effect lists" in verdict["error"]


def test_malformed_bundle_is_an_error(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_text("{not json", encoding="utf-8")
    result = runner.invoke(app, ["check", "--scenario", SCENARIO, "--bundle", str(bundle)])
    assert result.exit_code == 2
    assert "VERDICT: ERROR" in result.output


def test_bundle_with_swapped_artifacts_is_rejected(tmp_path):
    bundle = tmp_path / "bundle"
    captured = runner.invoke(app, [
        "capture",
        "--before", str(PAYMENT / "before-snapshot.json"),
        "--after", str(PAYMENT / "after-snapshot.json"),
        "--out", str(bundle),
    ])
    assert captured.exit_code == 0
    # Swap in a different (valid) after-artifact behind the manifest's back.
    swapped = (DEFECTS / "invalid-transition" / "after-snapshot.json").read_text(encoding="utf-8")
    (bundle / "after-snapshot.json").write_text(swapped, encoding="utf-8")
    result = runner.invoke(app, ["check", "--scenario", SCENARIO, "--bundle", str(bundle)])
    assert result.exit_code == 2
    assert "modified after capture" in result.output


def test_bundle_records_whether_the_event_log_cross_check_ran(tmp_path):
    without = tmp_path / "without-events"
    assert _capture(without).exit_code == 0
    manifest = _manifest(without)
    # The event COUNTS come from the snapshots' own events table and are not
    # evidence that anything was cross-checked; the flags are.
    assert manifest["after_events"] == 1
    assert manifest["before_events_cross_checked"] is False
    assert manifest["after_events_cross_checked"] is False

    with_events = tmp_path / "with-events"
    assert _capture(
        with_events,
        "--before-events", str(PAYMENT / "before-events.jsonl"),
        "--after-events", str(PAYMENT / "after-events.jsonl"),
    ).exit_code == 0
    manifest = _manifest(with_events)
    assert manifest["before_events_cross_checked"] is True
    assert manifest["after_events_cross_checked"] is True


def test_bundle_cannot_claim_a_cross_check_that_never_ran(tmp_path):
    bundle = tmp_path / "bundle"
    assert _capture(bundle).exit_code == 0
    manifest = _manifest(bundle)
    manifest["before_events_cross_checked"] = True
    manifest["after_events_cross_checked"] = True
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = runner.invoke(app, ["check", "--scenario", SCENARIO, "--bundle", str(bundle)])
    assert result.exit_code == 2
    assert "before_events_cross_checked" in result.output


def test_verdict_states_whether_the_event_log_cross_check_ran():
    plain = runner.invoke(app, [
        "check", "--scenario", SCENARIO,
        "--before", str(PAYMENT / "before-snapshot.json"),
        "--after", str(PAYMENT / "after-snapshot.json"),
    ])
    assert plain.exit_code == 0
    assert "Event-log cross-check: before NOT CHECKED" in plain.output

    with_logs = runner.invoke(app, [
        "check", "--scenario", SCENARIO,
        "--before", str(PAYMENT / "before-snapshot.json"),
        "--after", str(PAYMENT / "after-snapshot.json"),
        "--before-events", str(PAYMENT / "before-events.jsonl"),
        "--after-events", str(PAYMENT / "after-events.jsonl"),
        "--json",
    ])
    assert with_logs.exit_code == 0
    artifacts = json.loads(with_logs.stdout)["artifacts"]
    assert artifacts["before_events_cross_checked"] is True
    assert artifacts["after_events_cross_checked"] is True


def test_duplicate_key_in_a_bundle_manifest_is_rejected(tmp_path):
    bundle = tmp_path / "bundle"
    assert _capture(bundle).exit_code == 0
    (bundle / "manifest.json").write_text(
        with_duplicate_key(_manifest(bundle), "after_state_hash", "0" * 64), encoding="utf-8"
    )
    result = runner.invoke(app, ["check", "--scenario", SCENARIO, "--bundle", str(bundle)])
    assert result.exit_code == 2
    assert "duplicate JSON object key 'after_state_hash'" in result.output


def test_duplicate_key_in_a_stored_verdict_is_rejected(tmp_path):
    produced = runner.invoke(app, [
        "check", "--scenario", SCENARIO,
        "--before", str(PAYMENT / "before-snapshot.json"),
        "--after", str(DEFECTS / "duplicate-payment" / "after-snapshot.json"),
        "--json",
    ])
    assert produced.exit_code == 1
    # First "status" reads pass, the real one still reads fail: whichever half
    # a reader trusts, explain must refuse the file rather than pick one.
    verdict_file = tmp_path / "verdict.json"
    verdict_file.write_text(
        with_duplicate_key(json.loads(produced.stdout), "status", "pass"), encoding="utf-8"
    )
    result = runner.invoke(app, ["explain", str(verdict_file)])
    assert result.exit_code == 2
    assert "duplicate JSON object key 'status'" in result.output


def test_an_unencodable_artifact_still_produces_a_machine_readable_verdict(tmp_path):
    # `check --json` promises a statediff.verdict.v1 object for EVERY failure
    # mode. An escaped lone surrogate in `world_hash` used to reach the
    # hash-mismatch message, which cannot be UTF-8 encoded, so the command died
    # writing the verdict: stdout carried nothing at all and the exit code read
    # 1, an ordinary failing run, rather than 2 for a pair never evaluated.
    raw = json.loads((PAYMENT / "after-snapshot.json").read_text(encoding="utf-8"))
    raw["world_hash"] = "\ud800" + "0" * 59
    after = tmp_path / "surrogate-hash.json"
    after.write_text(json.dumps(raw, ensure_ascii=True, indent=2), encoding="utf-8")

    result = runner.invoke(app, [
        "check", "--scenario", SCENARIO,
        "--before", str(PAYMENT / "before-snapshot.json"),
        "--after", str(after),
        "--json",
    ])
    assert result.exit_code == 2
    verdict = json.loads(result.stdout)
    assert verdict["format"] == "statediff.verdict.v1"
    assert verdict["status"] == "error"
    assert "JSON/Unicode domain" in verdict["error"]


def test_a_bundle_refuses_event_logs_from_outside_it(tmp_path):
    # The two event-log options used to be overwritten by the manifest's
    # silently, so a caller who supplied one that disagreed with the bundle got
    # a passing verdict whose cross-check flags read `true` over a file that was
    # never opened: a path that did not exist at all was enough to produce one.
    # Refused rather than honoured, because the manifest fingerprints exactly
    # the files inside the bundle. An artifact from outside is covered by no
    # fingerprint and is not there when the same bundle is checked again, so
    # honouring it would cost a bundle the one thing it exists to give: the same
    # input set reproducing the same verdict.
    bundle = tmp_path / "bundle"
    assert _capture(
        bundle,
        "--before-events", str(PAYMENT / "before-events.jsonl"),
        "--after-events", str(PAYMENT / "after-events.jsonl"),
    ).exit_code == 0
    assert runner.invoke(app, ["check", "--scenario", SCENARIO, "--bundle", str(bundle)]).exit_code == 0

    for extra in (
        ["--before-events", str(tmp_path / "does-not-exist.jsonl")],
        ["--after-events", str(PAYMENT / "after-events.jsonl")],
        ["--before-events", str(PAYMENT / "before-events.jsonl"),
         "--after-events", str(PAYMENT / "after-events.jsonl")],
    ):
        result = runner.invoke(app, ["check", "--scenario", SCENARIO, "--bundle", str(bundle), *extra])
        assert result.exit_code == 2, extra
        assert "not both" in result.output, extra


def test_bundle_path_escape_is_rejected(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest = {
        "format": "statediff.bundle.v1",
        "files": {
            "before_snapshot": "../outside.json",
            "after_snapshot": "after-snapshot.json",
        },
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = runner.invoke(app, ["check", "--scenario", SCENARIO, "--bundle", str(bundle)])
    assert result.exit_code == 2
    assert "escapes the bundle directory" in result.output


def _run_bad_scenario(tmp_path, name: str, body: str):
    bad = tmp_path / f"{name}.yaml"
    bad.write_text(body, encoding="utf-8")
    return runner.invoke(app, [
        "check", "--scenario", str(bad),
        "--before", str(PAYMENT / "before-snapshot.json"),
        "--after", str(PAYMENT / "after-snapshot.json"),
        "--json",
    ])


def test_a_scenario_naming_an_unencodable_event_type_still_emits_a_verdict(tmp_path):
    # A scenario file can escape a lone surrogate into an event type. The refusal
    # names it, and a bare '{type}' used to carry the unencodable value into the
    # error verdict, so `check --json` died writing it: stdout was empty and the
    # exit code read 1, an ordinary failing run, rather than 2 for one never
    # evaluated. The value is quoted with !r now, so the verdict is writable.
    result = _run_bad_scenario(
        tmp_path, "surrogate-type",
        "scenario: statediff.scenario.v1\n"
        "id: SD-SUR\n"
        "title: surrogate type\n"
        "adapter: silobench\n"
        "effects:\n"
        "  expected:\n"
        '    - event_exists: {id: e, type: "\\uD800", count: 1}\n',
    )
    assert result.exit_code == 2
    verdict = json.loads(result.stdout)
    assert verdict["status"] == "error"
    assert verdict["format"] == "statediff.verdict.v1"
    assert "unknown event type" in verdict["error"]


def test_a_scenario_id_with_a_lone_surrogate_still_emits_a_verdict(tmp_path):
    # `id` is written into both the report and the verdict JSON. A plain string
    # is not surrogate-checked by pydantic, so it reached output and killed the
    # command; it is refused at parse now, and the error verdict names the file.
    result = _run_bad_scenario(
        tmp_path, "surrogate-id",
        "scenario: statediff.scenario.v1\n"
        'id: "\\uD800"\n'
        "title: surrogate id\n"
        "adapter: silobench\n"
        "effects:\n"
        "  expected:\n"
        "    - count_delta: {id: c, table: payments, added: 1}\n",
    )
    assert result.exit_code == 2
    verdict = json.loads(result.stdout)
    assert verdict["status"] == "error"
    assert verdict["format"] == "statediff.verdict.v1"


def test_a_cyclic_alias_scenario_still_emits_a_machine_readable_verdict(tmp_path):
    # A self-referential payload alias used to raise RecursionError out of the
    # parser, so `check --json` died with a traceback instead of the promised
    # error verdict. Guarded, it is an ordinary scenario error.
    result = _run_bad_scenario(
        tmp_path, "cyclic",
        "scenario: statediff.scenario.v1\n"
        "id: SD-CYCLE\n"
        "title: cyclic\n"
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
    )
    assert result.exit_code == 2
    verdict = json.loads(result.stdout)
    assert verdict["status"] == "error"
    assert verdict["format"] == "statediff.verdict.v1"
