"""Derive the committed defect fixtures from the captured baselines.

Every variant models a buggy DOWNSTREAM INTEGRATION, not file corruption: all
three hashes and the counters invariants are recomputed so the artifact is
internally consistent and only its semantics are wrong. Two extra variants
(`broken-hash`, `broken-counters`) are deliberately inconsistent to exercise
the fail-closed `error` path.

Deterministic: each defect class is a pure function of (baseline bytes, seed).
Seed 0 produces the committed variants; CI regenerates them and byte-compares.
Usage: python tools/inject_defects.py [--out fixtures/defects]
"""

from __future__ import annotations

import argparse
import copy
import json
import random
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parent.parent
BASELINE = REPO / "fixtures" / "baseline"

import sys

sys.path.insert(0, str(REPO / "src"))
from statediff.adapter import compute_hashes  # noqa: E402


Raw = dict[str, Any]


def _load(path: Path) -> Raw:
    return json.loads(path.read_text(encoding="utf-8"))


def _counter(raw: Raw, name: str, value: int) -> None:
    for row in raw["tables"]["counters"]:
        if row["name"] == name:
            row["value"] = value
            return
    raise KeyError(name)


def _rehash(raw: Raw) -> Raw:
    raw.update(compute_hashes(raw["meta"], raw["tables"]))
    return raw


def _events_jsonl(raw: Raw) -> str:
    meta = raw["meta"]
    events = raw["tables"]["events"]
    header = {
        "format": "event-log.v1",
        "seed": meta["seed"],
        "schema_version": meta["schema_version"],
        "docs_outage": meta["docs_outage"],
        "events": len(events),
    }
    lines = [json.dumps(header, ensure_ascii=False, separators=(",", ":"))]
    for row in events:
        record = {key: value for key, value in row.items() if key != "payload_json"}
        record["payload"] = json.loads(row["payload_json"])
        lines.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines) + "\n"


# --- the six defect classes -------------------------------------------------

def duplicate_payment(rng: random.Random) -> tuple[Raw, dict]:
    """FLAGSHIP timeout-after-commit: the connector's release call timed out
    after the commit, the retry wrote a second payment, and the audit write of
    the retry was lost. Two payment rows, ONE audit event."""
    raw = _load(BASELINE / "payment" / "after-snapshot.json")
    first = raw["tables"]["payments"][0]
    second = dict(first)
    second["payment_id"] = "PAY-0002"
    minute, second_of_minute = divmod(rng.randint(2, 120), 60)
    second["released_ts"] = f"2026-01-05T09:{minute:02d}:{second_of_minute:02d}.000Z"
    raw["tables"]["payments"].append(second)
    _counter(raw, "payment", 2)
    _counter(raw, "mutation_seq", 2)
    raw["meta"]["mutation_seq"] = 2
    manifest = {
        "defect_class": "duplicate-payment",
        "derived_from": "baseline/payment/after-snapshot.json",
        "before": "baseline/payment/before-snapshot.json",
        "scenario": "scenarios/payment-release.yaml",
        "expect": {"status": "fail", "failing_checks_include": [
            "at-most-one-payment-per-invoice", "one-payment", "no-second-payment",
        ]},
    }
    return _rehash(raw), manifest


def missing_audit_event(rng: random.Random) -> tuple[Raw, dict]:
    """The payment committed but its PAYMENT_RELEASED audit write was lost."""
    raw = _load(BASELINE / "payment" / "after-snapshot.json")
    raw["tables"]["events"] = []
    _counter(raw, "event", 0)
    manifest = {
        "defect_class": "missing-audit-event",
        "derived_from": "baseline/payment/after-snapshot.json",
        "before": "baseline/payment/before-snapshot.json",
        "scenario": "scenarios/payment-release.yaml",
        "expect": {"status": "fail", "failing_checks_include": ["audit-payment"]},
    }
    return _rehash(raw), manifest


def invalid_transition(rng: random.Random) -> tuple[Raw, dict]:
    """The invoice ended in a state the policy does not permit after release."""
    wrong_status = rng.choice(["rejected", "received", "matched", "approved_for_payment"])
    raw = _load(BASELINE / "payment" / "after-snapshot.json")
    for row in raw["tables"]["invoices"]:
        if row["invoice_id"] == "INV-2026-00347":
            row["status"] = wrong_status
    manifest = {
        "defect_class": "invalid-transition",
        "derived_from": "baseline/payment/after-snapshot.json",
        "before": "baseline/payment/before-snapshot.json",
        "scenario": "scenarios/payment-release.yaml",
        "expect": {"status": "fail", "failing_checks_include": ["invoice-paid"]},
    }
    return _rehash(raw), manifest


def unexplained_vendor_mutation(rng: random.Random) -> tuple[Raw, dict]:
    """Something also edited an unrelated vendor row during the run."""
    raw = _load(BASELINE / "payment" / "after-snapshot.json")
    filler_vendors = [row for row in raw["tables"]["erp_vendors"] if row["erp_vendor_id"].startswith("V-20")]
    victim = rng.choice(filler_vendors)
    victim["payment_terms_days"] = victim["payment_terms_days"] + rng.choice([7, 14, 15, 30])
    manifest = {
        "defect_class": "unexplained-vendor-mutation",
        "derived_from": "baseline/payment/after-snapshot.json",
        "before": "baseline/payment/before-snapshot.json",
        "scenario": "scenarios/payment-release.yaml",
        "expect": {"status": "fail", "failing_checks_include": ["unexplained"]},
    }
    return _rehash(raw), manifest


def rewritten_history(rng: random.Random) -> tuple[Raw, dict]:
    """A historical audit event was edited after the fact. Evaluated against
    the MID baseline, whose event history is non-empty: tampering over an
    empty history is undetectable by a prefix check, which is why MID exists."""
    raw = _load(BASELINE / "hold" / "after-snapshot.json")
    first_event = raw["tables"]["events"][0]
    mutation = rng.choice(["actor", "note", "ts"])
    if mutation == "actor":
        first_event["actor"] = "ap_approver"
    elif mutation == "ts":
        first_event["ts"] = "2026-01-05T08:59:59.000Z"
    else:
        payload = json.loads(first_event["payload_json"])
        payload["note"] = "routine data check"
        first_event["payload_json"] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    manifest = {
        "defect_class": "rewritten-history",
        "derived_from": "baseline/hold/after-snapshot.json",
        "before": "baseline/hold/mid-snapshot.json",
        "scenario": "scenarios/hold-compensation.yaml",
        "expect": {"status": "fail", "failing_checks_include": ["append_only"]},
    }
    return _rehash(raw), manifest


def uncompensated_hold(rng: random.Random) -> tuple[Raw, dict]:
    """The release never committed: the hold is still active and its
    HOLD_RELEASED audit event does not exist, yet the operator believes the
    check was completed."""
    raw = _load(BASELINE / "hold" / "after-snapshot.json")
    for row in raw["tables"]["holds"]:
        if row["hold_id"] == "HOLD-0003":
            row["active"] = 1
            row["released_by"] = None
            row["released_ts"] = None
    raw["tables"]["events"] = [
        row for row in raw["tables"]["events"] if row["type"] != "HOLD_RELEASED"
    ]
    _counter(raw, "event", 1)
    _counter(raw, "mutation_seq", 1)
    raw["meta"]["mutation_seq"] = 1
    manifest = {
        "defect_class": "uncompensated-hold",
        "derived_from": "baseline/hold/after-snapshot.json",
        "before": "baseline/hold/before-snapshot.json",
        "scenario": "scenarios/hold-compensation.yaml",
        "expect": {"status": "fail", "failing_checks_include": ["hold-pair-nets-out"]},
    }
    return _rehash(raw), manifest


DEFECTS: dict[str, Callable[[random.Random], tuple[Raw, dict]]] = {
    "duplicate-payment": duplicate_payment,
    "missing-audit-event": missing_audit_event,
    "invalid-transition": invalid_transition,
    "unexplained-vendor-mutation": unexplained_vendor_mutation,
    "rewritten-history": rewritten_history,
    "uncompensated-hold": uncompensated_hold,
}


# --- deliberately inconsistent artifacts (the `error` path) -----------------

def broken_hash() -> tuple[Raw, dict]:
    raw = _load(BASELINE / "payment" / "after-snapshot.json")
    raw["tables"]["payments"][0]["amount_cents"] = 1
    manifest = {
        "defect_class": "broken-hash",
        "derived_from": "baseline/payment/after-snapshot.json",
        "before": "baseline/payment/before-snapshot.json",
        "scenario": "scenarios/payment-release.yaml",
        "expect": {"status": "error", "error_contains": "hash mismatch"},
    }
    return raw, manifest


def broken_counters() -> tuple[Raw, dict]:
    raw = _load(BASELINE / "payment" / "after-snapshot.json")
    _counter(raw, "payment", 5)
    manifest = {
        "defect_class": "broken-counters",
        "derived_from": "baseline/payment/after-snapshot.json",
        "before": "baseline/payment/before-snapshot.json",
        "scenario": "scenarios/payment-release.yaml",
        "expect": {"status": "error", "error_contains": "counter payment"},
    }
    return _rehash(raw), manifest


ERROR_ARTIFACTS = {"broken-hash": broken_hash, "broken-counters": broken_counters}

RUN_CONTEXT = {
    "modeled": True,
    "note": (
        "Modeled run context for the flagship regression, not a live capture: "
        "what the operator saw while the world diverged."
    ),
    "agent_answer": "Payment for INV-2026-00347 released successfully; approval APR-0001 completed.",
    "tool_log": [
        {"call": 1, "tool": "erp_release_payment", "args": {"invoice_id": "INV-2026-00347"},
         "outcome": "timeout", "note": "gateway timeout AFTER the commit; response lost"},
        {"call": 2, "tool": "erp_release_payment", "args": {"invoice_id": "INV-2026-00347"},
         "outcome": "ok", "note": "blind retry by the connector; second payment row written"},
    ],
    "correlation": {
        "invoice_id": "INV-2026-00347",
        "approval_id": "APR-0001",
        "payment_ids": ["PAY-0001", "PAY-0002"],
        "audit_events": ["EVT-0001 (PAYMENT_RELEASED, only one exists)"],
    },
}


def _write(path: Path, text: str) -> None:
    # newline="\n" so the output bytes are identical on every platform; the
    # committed variants are byte-compared against regeneration in CI.
    path.write_text(text, encoding="utf-8", newline="\n")


def write_variant(outdir: Path, raw: Raw, manifest: dict) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    _write(outdir / "after-snapshot.json", json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
    _write(outdir / "after-events.jsonl", _events_jsonl(raw))
    _write(outdir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


def write_all(out_root: Path, seed: int = 0) -> None:
    for name, build in DEFECTS.items():
        raw, manifest = build(random.Random(seed))
        write_variant(out_root / name, raw, manifest)
        if name == "duplicate-payment":
            _write(out_root / name / "run-context.json", json.dumps(RUN_CONTEXT, ensure_ascii=False, indent=2) + "\n")
    for name, build in ERROR_ARTIFACTS.items():
        raw, manifest = build()
        write_variant(out_root / name, raw, manifest)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(REPO / "fixtures" / "defects"))
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    write_all(Path(args.out), seed=args.seed)
    print(f"wrote defect variants (seed {args.seed}) to {args.out}")


if __name__ == "__main__":
    main()
