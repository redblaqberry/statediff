"""Capture the committed baseline fixtures from a real SiloBench checkout.

Runs SiloBench's own CLI and stdio MCP servers; nothing here fabricates state.
Requires: a SiloBench checkout, pnpm on PATH, dependencies installed there.
The checkout is located by `--silobench PATH`, else `$SILOBENCH_REPO`, else the
sibling directories `../silobench` and `../02-silobench`. The siblings are only
a fallback for the side-by-side layout this was written in; the flag and the
env var are what make it run against any other layout.

Discipline:
- The upstream checkout must be CLEAN (no uncommitted changes); its commit id
  is recorded in `fixtures/baseline/capture-provenance.json`. Pass
  --allow-dirty only for local experiments whose output must not be committed.
- Everything is exported into a STAGING directory first, validated with the
  full statediff adapter (hash recompute, counters, meta, snapshot/JSONL
  cross-checks) plus exact business assertions, and promoted into
  `fixtures/baseline/` only after EVERY sequence has passed. A failed run
  leaves the committed fixtures untouched.

Sequences, each from a fresh seeded world:
  payment: TASK-10 release by ap_approver           -> baseline/payment/{before,after}
  hold:    place + release a data_mismatch hold by  -> baseline/hold/{before,mid,after}
           ap_clerk (mid = after placement, so the
           append-only defect has non-empty history)
  schema2: seeded world under export schema v2      -> baseline/schema2/{before}
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "fixtures" / "baseline"

sys.path.insert(0, str(REPO_ROOT / "src"))
from statediff.adapter import (  # noqa: E402
    cross_validate_event_log,
    load_event_log,
    load_pair,
    load_snapshot,
)

PROTOCOL_VERSION = "2025-06-18"
READ_TIMEOUT_S = 90

# What identifies a directory as a SiloBench checkout: the stdio server entry
# point this tool drives.
SILOBENCH_MARKER = Path("packages") / "servers" / "src" / "bin" / "erp-stdio.ts"

# SiloBench's committed golden hashes for the seeded world; the captures must
# reproduce them or something upstream changed.
SEED_GOLDEN_V1 = "38d60e95a46f0f488c7a594b045df7110b774ed83db6aac35670b4720369a866"
SEED_GOLDEN_V2 = "c6b1bcd35a594ddd20d5fdd98310c764db894ace9914b83ec53b4f0101b2cfa4"


def find_silobench(explicit: str | None = None) -> Path:
    """--silobench, else $SILOBENCH_REPO, else a sibling checkout.

    A location given explicitly is never silently skipped: if it is not a
    checkout, that is an error naming the path, rather than a fallback to
    whatever happens to sit next to this repo. PRESENCE decides that, not
    truthiness: read for truthiness, `--silobench ""` counted as no flag at
    all, and the run fell through to the environment or to whichever sibling
    happened to exist, capturing committed baselines from a checkout the
    operator never named while `capture-provenance.json` recorded that
    checkout's commit as though it had been chosen.
    """
    named = explicit if explicit is not None else os.environ.get("SILOBENCH_REPO")
    if named is not None:
        if not named.strip():
            sys.exit(
                "a SiloBench checkout was named but the path is empty; pass a real path to "
                "--silobench / SILOBENCH_REPO, or omit it to probe the sibling layout"
            )
        candidate = Path(named)
        if not (candidate / SILOBENCH_MARKER).exists():
            sys.exit(f"{candidate} is not a SiloBench checkout (no {SILOBENCH_MARKER})")
        return candidate.resolve()
    siblings = [REPO_ROOT.parent / "silobench", REPO_ROOT.parent / "02-silobench"]
    for candidate in siblings:
        if (candidate / SILOBENCH_MARKER).exists():
            return candidate.resolve()
    sys.exit(
        "SiloBench checkout not found; pass --silobench PATH or set SILOBENCH_REPO "
        f"(probed {', '.join(str(sibling) for sibling in siblings)})"
    )


def find_pnpm() -> str:
    pnpm = shutil.which("pnpm")
    if not pnpm:
        sys.exit("pnpm not found on PATH")
    return pnpm


PNPM = None  # resolved in main
SILOBENCH = None


def upstream_provenance(allow_dirty: bool) -> dict:
    def git(*args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(SILOBENCH), *args], capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            sys.exit(f"git {' '.join(args)} failed in {SILOBENCH}: {proc.stderr}")
        return proc.stdout.strip()

    commit = git("rev-parse", "HEAD")
    dirty = bool(git("status", "--porcelain"))
    if dirty and not allow_dirty:
        sys.exit(
            f"upstream checkout {SILOBENCH} has uncommitted changes; captures from a dirty "
            "tree must not become committed baselines (pass --allow-dirty for local experiments)"
        )
    return {
        "format": "statediff.capture-provenance.v1",
        "upstream_commit": commit,
        "upstream_dirty": dirty,
        "protocol_version": PROTOCOL_VERSION,
        "tool": "tools/capture_baselines.py",
    }


def run_cli(*args: str) -> str:
    proc = subprocess.run(
        [PNPM, "silobench", *args],
        cwd=SILOBENCH, capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        sys.exit(f"silobench {' '.join(args)} failed:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


def is_response_to(msg: object, expect_id: int) -> bool:
    """Whether `msg` claims to be the JSON-RPC response to `expect_id`.

    The id is compared by TYPE as well as value. Python's `True == 1` and
    `1.0 == 1` are both true, so a plain equality test would let a boolean or
    float id answer a request that used an integer one, and the capture would
    be promoted as a conversation it never actually had.
    """
    if not isinstance(msg, dict):
        return False
    value = msg.get("id")
    return isinstance(value, int) and not isinstance(value, bool) and value == expect_id


def response_result(system: str, msg: dict, expect_id: int) -> dict:
    """The `result` object of a well-framed JSON-RPC response.

    Framing is checked by value: a message without `jsonrpc: "2.0"`, or one
    whose result is not an object, is not a protocol-valid response, and the
    fixtures README calls what happens here a validated conversation.
    """
    if msg.get("jsonrpc") != "2.0":
        sys.exit(f"{system} answered id {expect_id} without jsonrpc 2.0 framing: {msg}")
    result = msg.get("result")
    if not isinstance(result, dict):
        sys.exit(f"{system} answered id {expect_id} with a non-object result: {result!r}")
    return result


def validate_initialize_result(system: str, init: dict) -> None:
    """The negotiated session must be the exact protocol version this tool
    records in `capture-provenance.json`, from a server that identified itself.

    Types and values, not truthiness: any truthy `protocolVersion` of any shape
    used to be accepted, so a session the provenance file misdescribes could be
    promoted as validated.
    """
    version = init.get("protocolVersion")
    if version != PROTOCOL_VERSION:
        sys.exit(
            f"{system} negotiated protocol version {version!r}, not the "
            f"{PROTOCOL_VERSION!r} this capture records as provenance"
        )
    server_info = init.get("serverInfo")
    if not isinstance(server_info, dict) or not isinstance(server_info.get("name"), str):
        sys.exit(f"{system} initialize response has no serverInfo name: {init}")
    if not isinstance(init.get("capabilities"), dict):
        sys.exit(f"{system} initialize response has no capabilities object: {init}")


def validate_tool_result(tool: str, result: dict) -> None:
    """`isError` is what decides whether the mutation happened, so only a real
    boolean answers it. Read for truthiness, `0` and `false` both counted as
    success while `"false"` counted as a failure: neither is a reading of what
    the server actually said, so a non-boolean is a protocol violation here.

    The field itself is OPTIONAL in the MCP tool-result schema and its absence
    is DEFINED as false, so the default below is the protocol's own reading and
    not a lenient one. Demanding the key would reject conformant servers, which
    is why this refuses the type and never the omission; `fixtures/README.md`
    describes the same rule.
    """
    is_error = result.get("isError", False)
    if not isinstance(is_error, bool):
        sys.exit(f"tool {tool} returned a non-boolean isError {is_error!r}: {json.dumps(result)[:500]}")
    if is_error:
        sys.exit(f"tool {tool} returned an error result: {json.dumps(result)[:500]}")


def mcp_call(system: str, principal: str, db: str, tool: str, arguments: dict) -> dict:
    """One validated JSON-RPC conversation with a SiloBench stdio server."""
    env = {**os.environ, "SILOBENCH_PRINCIPAL": principal, "SILOBENCH_DB": db}
    proc = subprocess.Popen(
        [PNPM, "tsx", "packages/servers/src/bin/" + system + "-stdio.ts"],
        cwd=SILOBENCH, env=env, text=True, encoding="utf-8",
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    lines: queue.Queue[str | None] = queue.Queue()

    def reader() -> None:
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=reader, daemon=True).start()

    def send(msg: dict) -> None:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def recv(expect_id: int) -> dict:
        while True:
            try:
                line = lines.get(timeout=READ_TIMEOUT_S)
            except queue.Empty:
                proc.kill()
                sys.exit(f"timeout waiting for response id {expect_id} from {system} stdio server")
            if line is None:
                proc.kill()
                sys.exit(f"{system} stdio server closed before response id {expect_id}: {proc.stderr.read()}")
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            # Anything not addressed to this request (notifications, server
            # requests) is demultiplexed away; only a claimed answer to it is
            # held to the framing rules.
            if not is_response_to(msg, expect_id):
                continue
            if "error" in msg:
                proc.kill()
                sys.exit(f"JSON-RPC error from {tool}: {msg['error']}")
            return response_result(system, msg, expect_id)

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "statediff-capture", "version": "0.1.0"},
        }})
        validate_initialize_result(system, recv(1))
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": tool, "arguments": arguments,
        }})
        result = recv(2)
        validate_tool_result(tool, result)
        return result
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        proc.wait(timeout=30)


def export(db: str, outdir: Path, stem: str) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)
    snap_path = outdir / f"{stem}-snapshot.json"
    events_path = outdir / f"{stem}-events.jsonl"
    run_cli("snapshot", "--db", db, "--out", str(snap_path))
    run_cli("events", "--db", db, "--out", str(events_path))
    return json.loads(snap_path.read_text(encoding="utf-8"))


def check(cond: bool, message: str) -> None:
    if not cond:
        sys.exit(f"post-capture assertion failed: {message}")


def validate_pair(outdir: Path, before_stem: str, after_stem: str) -> None:
    """Full adapter validation: hashes, counters, meta, JSONL cross-checks."""
    load_pair(
        outdir / f"{before_stem}-snapshot.json",
        outdir / f"{after_stem}-snapshot.json",
        outdir / f"{before_stem}-events.jsonl",
        outdir / f"{after_stem}-events.jsonl",
    )


def capture_payment(workdir: Path, staging: Path) -> None:
    db = str(workdir / "payment.db")
    out = staging / "payment"
    run_cli("seed", "--db", db)
    before = export(db, out, "before")
    check(before["state_hash"] == SEED_GOLDEN_V1, "payment before: seeded hash is not the published golden")
    check(before["tables"]["payments"] == [], "payment before: payments not empty")
    check(before["tables"]["events"] == [], "payment before: events not empty")

    mcp_call("erp", "ap_approver", db, "erp_release_payment", {"invoice_id": "INV-2026-00347"})

    after = export(db, out, "after")
    payments = after["tables"]["payments"]
    check(len(payments) == 1 and payments[0]["payment_id"] == "PAY-0001", "payment after: expected exactly PAY-0001")
    check(payments[0]["released_by"] == "ap_approver", "payment after: wrong releaser")
    check(payments[0]["approval_id"] == "APR-0001", "payment after: approval not linked")
    check(payments[0]["amount_cents"] == 210000, "payment after: wrong amount")
    invoice = next(r for r in after["tables"]["invoices"] if r["invoice_id"] == "INV-2026-00347")
    check(invoice["status"] == "paid", "payment after: invoice not paid")
    approval = next(r for r in after["tables"]["approval_requests"] if r["approval_id"] == "APR-0001")
    check(approval["status"] == "completed" and approval["completed_ts"], "payment after: approval not completed")
    events = after["tables"]["events"]
    check(len(events) == 1 and events[0]["type"] == "PAYMENT_RELEASED", "payment after: expected one PAYMENT_RELEASED")
    check(events[0]["actor"] == "ap_approver" and events[0]["system"] == "erp", "payment after: wrong event identity")
    payload = json.loads(events[0]["payload_json"])
    check(payload == {"payment_id": "PAY-0001", "amount_cents": 210000, "approval_id": "APR-0001"},
          f"payment after: unexpected event payload {payload}")
    check(after["state_hash"] != before["state_hash"], "payment after: state hash did not move")
    validate_pair(out, "before", "after")
    print("payment baseline captured:", after["state_hash"])


def capture_hold(workdir: Path, staging: Path) -> None:
    db = str(workdir / "hold.db")
    out = staging / "hold"
    run_cli("seed", "--db", db)
    before = export(db, out, "before")
    check(before["state_hash"] == SEED_GOLDEN_V1, "hold before: seeded hash is not the published golden")
    check(len(before["tables"]["holds"]) == 2, "hold before: expected the two seeded holds")

    mcp_call("erp", "ap_clerk", db, "erp_place_hold", {
        "invoice_id": "INV-2026-00318", "reason_code": "data_mismatch",
        "note": "Quantity check against PO-2026-0144 pending",
    })
    mid = export(db, out, "mid")
    check(len(mid["tables"]["holds"]) == 3, "hold mid: hold row missing")
    check(len(mid["tables"]["events"]) == 1 and mid["tables"]["events"][0]["type"] == "HOLD_PLACED",
          "hold mid: expected one HOLD_PLACED")

    mcp_call("erp", "ap_clerk", db, "erp_release_hold", {"hold_id": "HOLD-0003"})
    after = export(db, out, "after")
    released = next(r for r in after["tables"]["holds"] if r["hold_id"] == "HOLD-0003")
    check(released["active"] == 0, "hold after: HOLD-0003 still active")
    check(released["released_by"] == "ap_clerk", "hold after: wrong releaser")
    types = [e["type"] for e in after["tables"]["events"]]
    check(types == ["HOLD_PLACED", "HOLD_RELEASED"], f"hold after: unexpected event log {types}")
    check(all(e["actor"] == "ap_clerk" for e in after["tables"]["events"]), "hold after: wrong event identity")
    validate_pair(out, "before", "mid")
    validate_pair(out, "mid", "after")
    validate_pair(out, "before", "after")
    print("hold baseline captured:", after["state_hash"])


def capture_schema2(workdir: Path, staging: Path) -> None:
    db = str(workdir / "schema2.db")
    out = staging / "schema2"
    run_cli("seed", "--db", db, "--schema", "2")
    before = export(db, out, "before")
    check(before["meta"]["schema_version"] == 2, "schema2: wrong schema version")
    check(before["state_hash"] == SEED_GOLDEN_V2, "schema2: seeded hash is not the published v2 golden")
    loaded = load_snapshot(out / "before-snapshot.json")
    cross_validate_event_log(loaded, load_event_log(out / "before-events.jsonl"), "schema2 before")
    print("schema2 baseline captured:", before["state_hash"])


def promote(staging: Path, provenance: dict) -> None:
    """Build the complete new tree next to the live one, then swap it in by
    renaming whole directories, never file by file, so the promoted tree is
    always all-old or all-new and never a half-written mix.

    The swap itself is two renames and therefore not atomic. What is
    guaranteed: if the second rename fails, the old tree is renamed back before
    the error propagates; and if the process dies between the two renames (a
    kill or a power cut, where no handler can run), the old tree survives
    intact under `baseline.outgoing` and the recovery step below restores it on
    the next run. Only that window can leave `fixtures/baseline` missing, and
    it is recoverable rather than lost.
    """
    (staging / "capture-provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n",
    )
    FIXTURES.parent.mkdir(parents=True, exist_ok=True)
    incoming = FIXTURES.parent / "baseline.incoming"
    outgoing = FIXTURES.parent / "baseline.outgoing"
    if not FIXTURES.exists() and outgoing.exists():
        # A previous run died mid-swap and the only copy of the baseline is
        # parked here. Put it back BEFORE the leftover cleanup below, which
        # would otherwise delete it.
        print("recovering the baseline stranded at", outgoing)
        outgoing.rename(FIXTURES)
    for leftover in (incoming, outgoing):
        if leftover.exists():
            shutil.rmtree(leftover)
    shutil.copytree(staging, incoming)
    swapped_out = False
    if FIXTURES.exists():
        FIXTURES.rename(outgoing)
        swapped_out = True
    try:
        incoming.rename(FIXTURES)
    except OSError:
        # Nothing has taken the old tree's place yet, so rolling it back leaves
        # the checkout exactly as this function found it.
        if swapped_out:
            outgoing.rename(FIXTURES)
        raise
    if outgoing.exists():
        shutil.rmtree(outgoing)


def main() -> None:
    global PNPM, SILOBENCH
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-dirty", action="store_true",
                        help="capture from a dirty upstream tree (output must not be committed)")
    parser.add_argument("--silobench", metavar="PATH", default=None,
                        help="SiloBench checkout to capture from "
                             "(default: $SILOBENCH_REPO, else ../silobench or ../02-silobench)")
    args = parser.parse_args()
    PNPM = find_pnpm()
    SILOBENCH = find_silobench(args.silobench)
    provenance = upstream_provenance(args.allow_dirty)
    with tempfile.TemporaryDirectory() as workdir:
        staging = Path(workdir) / "staging"
        capture_payment(Path(workdir), staging)
        capture_hold(Path(workdir), staging)
        capture_schema2(Path(workdir), staging)
        promote(staging, provenance)
    print("all baselines validated and promoted into", FIXTURES)
    print("upstream commit:", provenance["upstream_commit"], "(dirty)" if provenance["upstream_dirty"] else "(clean)")


if __name__ == "__main__":
    main()
