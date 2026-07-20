"""Map a verdict to evaluation-gate style check results.

The shape is `{name, passed, detail}` with colon-namespaced names, compatible
with deterministic-check runners such as agent-eval-gate's CheckResult,
WITHOUT importing any gate package. The verdict-level entry is always
appended, so a failing or erroring verdict can never be projected as an
all-green check list even if a consumer filters by individual rule names.

`passed` answers "did this check block the run", which is the only question a
two-state boolean can answer. A `not_applicable` check cannot answer it in
either direction and is therefore NOT PROJECTED AT ALL. It arises only from an
allowed effect that never fired: allowed effects are never required, so `false`
would fail a run over an optional effect that was under no obligation to
happen, while `true` would hand the consumer a named check that verified
nothing. The consuming gate counts merged state checks toward how much a run
actually asserted, so a projected non-assertion inflates that count, and the
one thing this projection must never do is overstate what was checked.

Omission is stated rather than silent: the always-present verdict entry names
every check left out, and the verdict JSON keeps the full three-state list for
consumers that want it.
"""

from __future__ import annotations

from typing import Any

from .models import Verdict


def to_gate_checks(verdict: Verdict) -> list[dict[str, Any]]:
    checks = [
        {
            "name": f"statediff:{check.id}",
            "passed": check.status != "fail",
            "detail": check.detail,
        }
        for check in verdict.checks
        if check.status != "not_applicable"
    ]
    unfired = [check.id for check in verdict.checks if check.status == "not_applicable"]
    checks.append({
        "name": "statediff:verdict",
        "passed": verdict.status == "pass",
        "detail": (
            f"scenario {verdict.scenario_id}: {verdict.status}"
            + (f" ({verdict.error})" if verdict.error else "")
            + (
                f"; {len(unfired)} allowed effect(s) never fired and are not projected "
                f"as checks: {', '.join(unfired)}"
                if unfired else ""
            )
        ),
    })
    return checks
