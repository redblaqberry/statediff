"""The success metric: every planted defect class is detected by its intended
check across 100 stratified injector seeds, with zero false positives on the
clean pairs in every round; the committed variants byte-match regeneration."""

import filecmp
import json
import random

import pytest

from statediff.adapter import load_pair
from statediff.engine import evaluate, evaluate_paths
from statediff.scenario import load_scenario
from tests.conftest import FIXTURES
from tools.inject_defects import DEFECTS, write_all, write_variant

REPO = FIXTURES.parent
DEFECT_DIRS = FIXTURES / "defects"
CLASSES = list(DEFECTS)

SCENARIOS = {
    "scenarios/payment-release.yaml": load_scenario(REPO / "scenarios" / "payment-release.yaml"),
    "scenarios/hold-compensation.yaml": load_scenario(REPO / "scenarios" / "hold-compensation.yaml"),
}
CLEAN_PAIRS = [
    (SCENARIOS["scenarios/payment-release.yaml"], load_pair(
        FIXTURES / "baseline" / "payment" / "before-snapshot.json",
        FIXTURES / "baseline" / "payment" / "after-snapshot.json",
    )),
    (SCENARIOS["scenarios/hold-compensation.yaml"], load_pair(
        FIXTURES / "baseline" / "hold" / "before-snapshot.json",
        FIXTURES / "baseline" / "hold" / "after-snapshot.json",
    )),
]


def read_manifest(defect_dir):
    return json.loads((defect_dir / "manifest.json").read_text(encoding="utf-8"))


def evaluate_manifest(defect_dir):
    manifest = read_manifest(defect_dir)
    scenario = SCENARIOS[manifest["scenario"]]
    return manifest, evaluate_paths(
        scenario,
        FIXTURES / manifest["before"],
        defect_dir / "after-snapshot.json",
        after_events_path=defect_dir / "after-events.jsonl"
        if (defect_dir / "after-events.jsonl").exists() and manifest["expect"]["status"] != "error"
        else None,
    )


@pytest.mark.parametrize("name", sorted(path.name for path in DEFECT_DIRS.iterdir()))
def test_committed_defect_variant_produces_its_verdict(name):
    manifest, verdict = evaluate_manifest(DEFECT_DIRS / name)
    expect = manifest["expect"]
    assert verdict.status == expect["status"]
    if expect["status"] == "fail":
        failing = {check.id for check in verdict.checks if check.status == "fail"}
        assert set(expect["failing_checks_include"]) <= failing
    else:
        assert expect["error_contains"] in verdict.error


@pytest.mark.parametrize("seed", range(100))
def test_seed_matrix_every_class_every_seed(seed, tmp_path):
    # ALL six defect classes are exercised for EVERY seed (600 defective
    # evaluations total), so a class-specific regression cannot hide behind
    # seed stratification.
    for name in CLASSES:
        raw, manifest = DEFECTS[name](random.Random(seed))
        outdir = tmp_path / name
        write_variant(outdir, raw, manifest)

        scenario = SCENARIOS[manifest["scenario"]]
        verdict = evaluate_paths(
            scenario,
            FIXTURES / manifest["before"],
            outdir / "after-snapshot.json",
            after_events_path=outdir / "after-events.jsonl",
        )
        assert verdict.status == "fail", f"{name} seed {seed} was not detected"
        failing = {check.id for check in verdict.checks if check.status == "fail"}
        missing = set(manifest["expect"]["failing_checks_include"]) - failing
        assert not missing, f"{name} seed {seed}: intended checks did not fire: {missing}"

    # Zero false positives: the corrected artifacts stay green every round.
    for clean_scenario, clean_pair in CLEAN_PAIRS:
        clean = evaluate(clean_scenario, clean_pair)
        assert clean.status == "pass", f"clean {clean_scenario.id} regressed at seed {seed}"
        assert clean.unexplained == []


def _relative_files(root):
    return sorted(
        str(path.relative_to(root)).replace("\\", "/")
        for path in root.rglob("*") if path.is_file()
    )


def test_committed_variants_byte_match_regeneration(tmp_path):
    write_all(tmp_path, seed=0)
    committed_files = _relative_files(DEFECT_DIRS)
    regenerated_files = _relative_files(tmp_path)
    # Bidirectional: a new generator output that was never committed is drift
    # exactly like a stale committed file.
    assert committed_files == regenerated_files
    mismatches = [
        relative for relative in committed_files
        if not filecmp.cmp(DEFECT_DIRS / relative, tmp_path / relative, shallow=False)
    ]
    assert not mismatches, mismatches
