"""The statediff CLI: capture, check, explain.

Exit codes are part of the contract: 0 pass, 1 fail, 2 error (including any
artifact or scenario problem). With --json, stdout carries only the verdict
JSON and the human report goes to stderr, so output pipes cleanly.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Optional

import typer

from . import __version__
from .adapter import ArtifactError, load_pair, strict_json_loads
from .engine import error_verdict, evaluate
from .evidence import render
from .gate import to_gate_checks
from .models import Verdict
from .scenario import ScenarioError, load_scenario

app = typer.Typer(
    add_completion=False,
    help="State oracle for agent evaluation: grades before/after world snapshots against a scenario.",
)

EXIT_BY_STATUS = {"pass": 0, "fail": 1, "error": 2}


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", help="Print the version and exit."),
) -> None:
    if version:
        typer.echo(f"statediff {__version__}")
        raise typer.Exit(0)
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)


def _resolve_bundle(bundle: Path) -> tuple[Path, Path, Optional[Path], Optional[Path], dict]:
    manifest_path = bundle / "manifest.json"
    if not manifest_path.exists():
        raise ArtifactError(f"bundle {bundle} has no manifest.json")
    try:
        manifest = strict_json_loads(manifest_path.read_text(encoding="utf-8"), f"bundle {bundle} manifest")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"bundle {bundle}: unreadable manifest: {exc}") from exc
    except RecursionError as exc:
        # A manifest nested thousands of arrays deep exhausts the parser's stack;
        # RecursionError is not a JSONDecodeError, so it must be turned into an
        # error verdict here rather than escaping as a traceback and exit 1.
        raise ArtifactError(f"bundle {bundle}: manifest nested too deeply to parse") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != "statediff.bundle.v1":
        raise ArtifactError(f"bundle {bundle}: unknown manifest format")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ArtifactError(f"bundle {bundle}: manifest has no files mapping")

    bundle_root = bundle.resolve()

    def path_for(name: str) -> Optional[Path]:
        file_name = files.get(name)
        if file_name is None:
            return None
        if not isinstance(file_name, str):
            raise ArtifactError(f"bundle {bundle}: file entry {name} is not a string")
        resolved = (bundle / file_name).resolve()
        if bundle_root not in resolved.parents and resolved != bundle_root:
            raise ArtifactError(f"bundle {bundle}: file entry {name} escapes the bundle directory")
        return resolved

    before, after = path_for("before_snapshot"), path_for("after_snapshot")
    if before is None or after is None:
        raise ArtifactError(f"bundle {bundle}: manifest missing before/after snapshots")
    return before, after, path_for("before_events"), path_for("after_events"), manifest


def _verify_bundle_fingerprints(manifest: dict, pair) -> None:
    """The manifest fingerprints are a contract, not decoration: artifacts
    swapped after capture must be rejected. The cross-check flags are verified
    the same way, against the pair just reloaded from the bundle, so a manifest
    cannot claim a cross-check whose event logs are not there to run it."""
    actual = {
        "schema_version": pair.after.schema_version,
        "before_state_hash": pair.before.snapshot.state_hash,
        "after_state_hash": pair.after.snapshot.state_hash,
        "before_events": len(pair.before.events),
        "after_events": len(pair.after.events),
        "before_events_cross_checked": pair.before_events_cross_checked,
        "after_events_cross_checked": pair.after_events_cross_checked,
    }
    from .canonical import strict_equal

    for key, value in actual.items():
        if not strict_equal(manifest.get(key), value):
            raise ArtifactError(
                f"bundle manifest {key} ({manifest.get(key)!r}) does not match its artifacts "
                f"({value!r}); the bundle was modified after capture"
            )


@app.command()
def check(
    scenario: Path = typer.Option(..., help="Scenario YAML."),
    before: Optional[Path] = typer.Option(None, help="Before snapshot.v1 JSON."),
    after: Optional[Path] = typer.Option(None, help="After snapshot.v1 JSON."),
    before_events: Optional[Path] = typer.Option(None, help="Optional before event-log.v1 JSONL."),
    after_events: Optional[Path] = typer.Option(None, help="Optional after event-log.v1 JSONL."),
    bundle: Optional[Path] = typer.Option(None, help="Run bundle directory (from `statediff capture`)."),
    json_output: bool = typer.Option(False, "--json", help="Print the verdict JSON on stdout."),
    gate_output: bool = typer.Option(False, "--gate", help="Print gate-style check results JSON on stdout."),
) -> None:
    """Evaluate a scenario against a before/after artifact pair."""
    verdict = _checked_verdict(scenario, before, after, before_events, after_events, bundle)
    report = render(verdict)
    if json_output or gate_output:
        typer.echo(report, err=True, nl=False)
        payload = to_gate_checks(verdict) if gate_output else verdict.model_dump(by_alias=True)
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        typer.echo(report, nl=False)
    raise typer.Exit(EXIT_BY_STATUS[verdict.status])


def _checked_verdict(
    scenario: Path,
    before: Optional[Path],
    after: Optional[Path],
    before_events: Optional[Path],
    after_events: Optional[Path],
    bundle: Optional[Path],
) -> Verdict:
    """Every failure mode becomes a statediff.verdict.v1 error object, so
    --json and --gate consumers always receive machine-readable output and the
    exit code is always derived from a verdict."""
    scenario_id = scenario.stem
    try:
        scenario_model = load_scenario(scenario)
    except ScenarioError as exc:
        return error_verdict(scenario_id, f"scenario error: {exc}")
    try:
        provenance = scenario_model.provenance.model_dump()
        manifest = None
        if bundle is not None:
            # All four inputs, not just the snapshots. The event log paths used
            # to be overwritten by the manifest's silently, so a caller who
            # passed one that disagreed with the bundle got a verdict whose
            # cross-check flags read `true` over a file that was never opened
            # (a path that did not exist at all still produced a passing run).
            # Honouring the supplied path instead would break what a bundle is
            # for: the manifest fingerprints exactly the files inside it, so an
            # artifact from outside is neither covered by that check nor present
            # when the same bundle is re-checked later, and the same bundle
            # would stop reproducing the same verdict.
            if before or after or before_events or after_events:
                return error_verdict(
                    scenario_model.id,
                    "use either --bundle or --before/--after with their optional event logs, "
                    "not both: a bundle is the whole input set its manifest fingerprints",
                    title=scenario_model.title, provenance=provenance,
                )
            before, after, before_events, after_events, manifest = _resolve_bundle(bundle)
        elif before is None or after is None:
            return error_verdict(
                scenario_model.id, "check needs --before and --after (or --bundle)",
                title=scenario_model.title, provenance=provenance,
            )
        pair = load_pair(before, after, before_events, after_events)
        if manifest is not None:
            _verify_bundle_fingerprints(manifest, pair)
    except ArtifactError as exc:
        return error_verdict(
            scenario_model.id, str(exc), title=scenario_model.title,
            provenance=scenario_model.provenance.model_dump(),
        )
    return evaluate(scenario_model, pair)


@app.command()
def capture(
    before: Path = typer.Option(..., exists=True, dir_okay=False, help="Before snapshot.v1 JSON."),
    after: Path = typer.Option(..., exists=True, dir_okay=False, help="After snapshot.v1 JSON."),
    before_events: Optional[Path] = typer.Option(None, exists=True, dir_okay=False),
    after_events: Optional[Path] = typer.Option(None, exists=True, dir_okay=False),
    out: Path = typer.Option(..., file_okay=False, help="Bundle directory to create."),
) -> None:
    """Validate a pair of artifacts (formats, hashes, counters, cross-checks)
    and store it as a named run bundle."""
    try:
        pair = load_pair(before, after, before_events, after_events)
    except ArtifactError as exc:
        typer.echo(f"artifact error: {exc}", err=True)
        raise typer.Exit(2) from exc

    try:
        out.mkdir(parents=True, exist_ok=True)
        files = {"before_snapshot": "before-snapshot.json", "after_snapshot": "after-snapshot.json"}
        shutil.copyfile(before, out / files["before_snapshot"])
        shutil.copyfile(after, out / files["after_snapshot"])
        if before_events is not None:
            files["before_events"] = "before-events.jsonl"
            shutil.copyfile(before_events, out / files["before_events"])
        if after_events is not None:
            files["after_events"] = "after-events.jsonl"
            shutil.copyfile(after_events, out / files["after_events"])
        manifest = {
            "format": "statediff.bundle.v1",
            "files": files,
            "schema_version": pair.after.schema_version,
            "before_state_hash": pair.before.snapshot.state_hash,
            "after_state_hash": pair.after.snapshot.state_hash,
        # Counts of the snapshots' own event rows. They look like event-log
        # evidence and are not: the two flags below record whether the
        # snapshot/event-log cross-check actually ran, so a bundle captured
        # without event logs cannot be mistaken for a cross-validated one.
            "before_events": len(pair.before.events),
            "after_events": len(pair.after.events),
            "before_events_cross_checked": pair.before_events_cross_checked,
            "after_events_cross_checked": pair.after_events_cross_checked,
        }
        (out / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        # Writing the bundle is IO past the point of a valid input: an unwritable
        # destination, a full disk, or a failed copy is an infrastructure error,
        # not a bad artifact. It must still exit 2 (error), never fall through
        # Python's default uncaught-exception exit 1, which a caller reads as an
        # ordinary `fail`.
        typer.echo(f"could not write bundle to {out}: {exc}", err=True)
        raise typer.Exit(2) from exc
    cross_checked = "before+after" if before_events and after_events else (
        "before only" if before_events else "after only" if after_events else "NONE"
    )
    typer.echo(
        f"bundle written to {out} (state {manifest['after_state_hash'][:12]}..., "
        f"event-log cross-check: {cross_checked})"
    )


@app.command()
def explain(
    verdict_file: Path = typer.Argument(..., exists=True, dir_okay=False, help="A statediff.verdict.v1 JSON file."),
) -> None:
    """Render a stored verdict as the human narrative."""
    try:
        raw = strict_json_loads(verdict_file.read_text(encoding="utf-8"), str(verdict_file))
        verdict = Verdict.model_validate(raw)
    except Exception as exc:
        typer.echo(f"not a readable statediff.verdict.v1 file: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(render(verdict), nl=False)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
