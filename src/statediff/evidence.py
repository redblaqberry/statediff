"""Human rendering of a verdict: the concise diff narrative `explain` prints.

The row-to-event correlation block is rendered from the evidence a `correlated`
check produced, never re-derived here. A display-only join reads like a
finding while binding nothing, and this one used to print
"NO AUDIT EVENT EXISTS" underneath a PASS verdict: the report and the verdict
now come from the same computation and cannot contradict each other.
"""

from __future__ import annotations

from .models import Verdict

_MAX_VALUE = 100

# Padded to a fixed width so the status column stays aligned down the report.
_STATUS_MARKERS = {"pass": "PASS", "fail": "FAIL", "not_applicable": "N/A "}


def _compact(value: object) -> str:
    text = repr(value)
    return text if len(text) <= _MAX_VALUE else text[: _MAX_VALUE - 3] + "..."


def _cross_check_note(cross_checked: bool) -> str:
    return "verified against the event log" if cross_checked else "NOT CHECKED (no event log supplied)"


def _row_correlation_line(entry: dict) -> str:
    event_ids = entry.get("event_ids") or []
    if not event_ids:
        trail = "NO AUDIT EVENT EXISTS"
    else:
        trail = "audited by " + ", ".join(str(event_id) for event_id in event_ids)
        if entry.get("kind") == "row_not_correlated":
            trail += f" (want {entry.get('want')})"
    return f"  {entry.get('table')}[{entry.get('pk')}] -> {entry.get('event_type')}: {trail}"


def _correlation(verdict: Verdict) -> list[str]:
    rows: list[str] = []
    dangling: list[str] = []
    seen: set[tuple] = set()
    for check in verdict.checks:
        for entry in check.evidence:
            kind = entry.get("kind")
            if kind in {"correlated", "row_not_correlated"}:
                # Two effects may correlate the same rows against different
                # event types; only a repeat of the identical pairing is noise.
                key = (entry.get("table"), repr(entry.get("pk")), entry.get("event_type"))
                if key in seen:
                    continue
                seen.add(key)
                rows.append(_row_correlation_line(entry))
            elif kind == "event_names_missing_row":
                dangling.append(
                    f"  {entry.get('type')} {entry.get('event_id')} names "
                    f"{entry.get('table')}.{entry.get('payload_key')} "
                    f"{entry.get('value')!r}: NO SUCH ROW"
                )
    if not rows and not dangling:
        return []
    return ["", "Row/event correlation:", *rows, *dangling]


def render(verdict: Verdict) -> str:
    lines = [
        f"{verdict.scenario_id}: {verdict.title or ''}".rstrip(),
        f"VERDICT: {verdict.status.upper()}"
        + (f" ({verdict.error})" if verdict.error else ""),
    ]
    for check in verdict.checks:
        marker = _STATUS_MARKERS[check.status]
        lines.append(f"  [{marker}] {check.id} ({check.rule}, {check.list_name}): {check.detail}")
        if check.status == "fail":
            for entry in check.evidence[:5]:
                lines.append(f"      evidence: {_compact(entry)}")
            if len(check.evidence) > 5:
                lines.append(f"      ... and {len(check.evidence) - 5} more")
    if verdict.unexplained:
        lines.append(f"Unexplained changes ({len(verdict.unexplained)}):")
        for entry in verdict.unexplained[:10]:
            lines.append(f"  - {_compact(entry)}")
        if len(verdict.unexplained) > 10:
            lines.append(f"  ... and {len(verdict.unexplained) - 10} more")
    lines.extend(_correlation(verdict))
    if verdict.artifacts:
        art = verdict.artifacts
        lines.append(
            f"Artifacts: schema v{art.schema_version}, state {art.before_state_hash[:12]}... -> "
            f"{art.after_state_hash[:12]}..., events {art.before_events} -> {art.after_events}"
        )
        # Stated explicitly, always: the event counts above come from the
        # snapshots themselves, so without this line a reader would have no way
        # to tell a pair checked against its event logs from one where that
        # check never ran.
        lines.append(
            f"Event-log cross-check: before {_cross_check_note(art.before_events_cross_checked)}, "
            f"after {_cross_check_note(art.after_events_cross_checked)}"
        )
    return "\n".join(lines) + "\n"
