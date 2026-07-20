"""Justification engine: evaluates a scenario over a before/after pair.

The verdict is a pure function of (scenario, before, after). Failures:
unsatisfied expected effects, fired forbidden effects, an append-only
violation, or any unexplained diff atom. Anything the engine cannot evaluate
(bad scenario selectors, inconsistent artifacts) is an `error` verdict, which
fails downstream gates exactly like `fail` (fail-closed).
"""

from __future__ import annotations

from .adapter import ENVIRONMENT_TABLE, ArtifactError, ArtifactPair
from .diff import compute_diff, event_atom_id
from .models import ArtifactFingerprints, CheckOutcome, Verdict
from .rules import EVALUATORS
from .rules.base import RuleContext, event_evidence
from .scenario import RULE_MODELS, Effect, Scenario, ScenarioError, validate_effects


def error_verdict(
    scenario_id: str,
    message: str,
    title: str | None = None,
    provenance: dict | None = None,
) -> Verdict:
    return Verdict(
        scenario_id=scenario_id,
        title=title,
        status="error",
        error=message,
        provenance=provenance,
        checks=[
            CheckOutcome(
                id="artifact", rule="artifact", list_name="artifact",
                status="fail", detail=message,
            )
        ],
    )


def _scenario_error_verdict(scenario: Scenario, message: str) -> Verdict:
    # This builds the error verdict for a scenario that could not be evaluated, so
    # it must never raise itself: a caller who mutated `provenance` to None (or to
    # anything without a readable `model_dump`) would otherwise turn the handler
    # into a second, uncaught exception, and the internal failure would escape as
    # a traceback exactly where an error verdict was promised. Every field it reads
    # is taken defensively.
    provenance = getattr(scenario, "provenance", None)
    try:
        dumped = provenance.model_dump() if provenance is not None else None
    except Exception:  # noqa: BLE001
        dumped = None
    return error_verdict(
        getattr(scenario, "id", "") or "", message,
        title=getattr(scenario, "title", None),
        provenance=dumped,
    )


def evaluate(scenario: Scenario, pair: ArtifactPair) -> Verdict:
    try:
        return _evaluate(scenario, pair)
    except (ScenarioError, ArtifactError) as exc:
        return _scenario_error_verdict(scenario, str(exc))
    except Exception as exc:  # noqa: BLE001
        # The engine's contract is that it always returns a verdict, and that an
        # internal failure is an `error` (exit 2), never a `pass`. A rule
        # evaluator handed a spec that scenario validation did not anticipate can
        # raise something other than ScenarioError: a `table: []` mutated onto a
        # rule, say, makes a set-membership test raise `TypeError: unhashable type`.
        # Letting it escape would return a traceback and exit 1 where every other
        # unusable scenario yields an error verdict. Caught here as defence in
        # depth behind `validate_effects`, so the failure fails closed.
        return _scenario_error_verdict(scenario, f"internal error evaluating scenario: {exc}")


def evaluate_paths(
    scenario: Scenario,
    before_path,
    after_path,
    before_events_path=None,
    after_events_path=None,
) -> Verdict:
    """Load artifacts and evaluate; every artifact problem becomes an `error`
    verdict instead of an exception, so callers cannot accidentally treat a
    broken artifact as anything but a failure."""
    from .adapter import load_pair

    try:
        pair = load_pair(before_path, after_path, before_events_path, after_events_path)
    except ArtifactError as exc:
        return _scenario_error_verdict(scenario, str(exc))
    return evaluate(scenario, pair)


def _evaluator_for(effect: Effect):
    """Rule dispatch that fails closed for a scenario assembled in Python.

    `parse_scenario` rejects an unknown rule name and pairs every effect with
    the spec model of its own rule, so a file-loaded scenario can never reach
    here malformed. A library caller can build `Effect` directly, and there a
    bare `EVALUATORS[...]` lookup raises KeyError from OUTSIDE `evaluate`'s
    handler: the exported API would hand back a traceback where every other
    unusable scenario yields an `error` verdict, and an integration that
    catches nothing would crash instead of failing the run.
    """
    evaluator = EVALUATORS.get(effect.rule)
    model = RULE_MODELS.get(effect.rule)
    if evaluator is None or model is None:
        # The intersection, not either registry alone: a rule listed in one and
        # not the other cannot be evaluated, and reporting it as known here
        # would only move the crash one line down.
        known = sorted(set(EVALUATORS) & set(RULE_MODELS))
        raise ScenarioError(f"unknown rule '{effect.rule}' (known: {known})")
    if not isinstance(effect.spec, model):
        raise ScenarioError(
            f"effect for rule '{effect.rule}' carries a {type(effect.spec).__name__} spec, "
            f"not the {model.__name__} that rule reads"
        )
    return evaluator


def _evaluate(scenario: Scenario, pair: ArtifactPair) -> Verdict:
    # Every scenario-level invariant, re-checked on the object the engine was
    # actually handed. `parse_scenario` enforces these while reading a file, but
    # `model_construct` and `model_copy(update=...)` skip validation, so a library
    # caller could reach here with an empty effect set, an allowed-only scenario
    # that asserts nothing, a forbidden invariant rule, or a duplicate/reserved
    # effect id. Without this the runtime dispatch checked only rule name and spec
    # type, and a mutated in-memory spec that a file could never express evaluated
    # to `pass`.
    validate_effects(scenario.effects)

    diff = compute_diff(pair)
    ctx = RuleContext(pair=pair, diff=diff)

    # Evaluate every effect before assembling anything, so a scenario error in
    # ANY effect (including allowed ones) yields a clean error verdict.
    outcomes = [(effect, _evaluator_for(effect)(effect.spec, ctx)) for effect in scenario.effects]

    checks: list[CheckOutcome] = []
    covered: set = set()
    failed = False
    for effect, outcome in outcomes:
        if effect.list_name == "expected":
            status = "pass" if outcome.satisfied else "fail"
            detail = outcome.detail
            if outcome.satisfied:
                covered |= outcome.covered
        elif effect.list_name == "allowed":
            # Allowed effects are never required; they only justify atoms, and
            # only while their own constraint holds (conditional coverage). One
            # whose constraint did NOT hold justified nothing, so reporting it
            # as a pass would credit it with a predicate it never satisfied,
            # and reporting it as a failure would make an optional effect
            # mandatory. It gets the third status instead, and its detail says
            # the same thing the status does.
            if outcome.satisfied and outcome.covered:
                status = "pass"
                detail = f"allowed, coverage only: {outcome.detail}"
                covered |= outcome.covered
            else:
                # `and outcome.covered`: an allowed effect exists to justify atoms,
                # so it is a pass only when it actually justified one. Satisfaction
                # alone is not firing: an allowed `event_exists {count: 0}` or a
                # `count_delta {added: 0}` is satisfied by ABSENCE, covers nothing,
                # and used to be projected as a verified check (`passed: true`) with
                # no evidence. A satisfied effect that covered nothing did not fire,
                # so it takes the same not_applicable path as one that was not
                # satisfied at all.
                status = "not_applicable"
                detail = f"allowed, did not fire, so it justified nothing: {outcome.detail}"
        else:
            status = "fail" if outcome.satisfied else "pass"
            detail = f"forbidden: {outcome.detail}"
        if status == "fail":
            failed = True
        checks.append(CheckOutcome(
            id=effect.spec.id, rule=effect.rule, list_name=effect.list_name,
            status=status, detail=detail, evidence=outcome.evidence,
        ))

    append_status = "pass" if diff.append_only_ok else "fail"
    if not diff.append_only_ok:
        failed = True
    checks.append(CheckOutcome(
        id="append_only", rule="append_only", list_name="invariant",
        status=append_status, detail=diff.append_only_detail,
    ))

    profile_change_allowed = scenario.rules_config.profile_change == "allow"
    unexplained: list[dict] = []
    for atom in diff.atoms:
        if atom.table == ENVIRONMENT_TABLE and profile_change_allowed:
            continue
        if atom.atom_id not in covered:
            unexplained.append(atom.describe())
    for event in diff.suffix_events:
        if event_atom_id(event) not in covered:
            unexplained.append({**event_evidence(event), "kind": "event_appended"})
    sweep_status = "pass" if not unexplained else "fail"
    if unexplained:
        failed = True
    checks.append(CheckOutcome(
        id="unexplained", rule="unexplained_sweep", list_name="invariant",
        status=sweep_status,
        detail=(
            "every change is justified by an expected or allowed effect"
            if not unexplained
            else f"{len(unexplained)} change(s) no expected or allowed effect accounts for"
        ),
        evidence=unexplained,
    ))

    return Verdict(
        scenario_id=scenario.id,
        title=scenario.title,
        status="fail" if failed else "pass",
        checks=checks,
        unexplained=unexplained,
        artifacts=ArtifactFingerprints(
            schema_version=pair.after.schema_version,
            before_state_hash=pair.before.snapshot.state_hash,
            after_state_hash=pair.after.snapshot.state_hash,
            before_events=len(pair.before.events),
            after_events=len(pair.after.events),
            before_events_cross_checked=pair.before_events_cross_checked,
            after_events_cross_checked=pair.after_events_cross_checked,
        ),
        provenance=scenario.provenance.model_dump(),
    )
