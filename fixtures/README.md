# Fixture provenance

Everything under `baseline/` is a REAL capture from an unmodified SiloBench
checkout; nothing was hand-edited. Everything under `defects/` is DERIVED from
those captures by `tools/inject_defects.py` and models a buggy downstream
integration (a payment connector that retried after a timeout, an audit write
that got lost). SiloBench itself cannot produce the defect states, its ERP
enforces exactly-one-payment, which is precisely why an external state oracle
exists: a correct environment does not make every integration correct.

## Baseline capture procedure (`tools/capture_baselines.py`)

Requires a SiloBench checkout with dependencies installed and `pnpm` on PATH.
The checkout is located by `--silobench PATH`, else `$SILOBENCH_REPO`, else a
sibling `../silobench` or `../02-silobench` next to this repo. The driver:

1. Records the upstream commit id and REFUSES a dirty upstream tree (the
   result lands in `capture-provenance.json`; `--allow-dirty` exists only for
   local experiments whose output must not be committed).
2. Seeds a fresh throwaway database per sequence via `pnpm silobench seed --db <tmp>`.
3. Exports `snapshot.v1` and `event-log.v1` before, between, and after
   mutations via the silobench CLI, into a STAGING directory.
4. Performs every mutation through the real stdio MCP server
   (`packages/servers/src/bin/erp-stdio.ts`) with a validated JSON-RPC
   conversation. Validated means protocol values and types, never truthiness:
   a response is matched to its request id by type as well as value (Python's
   `True == 1` and `1.0 == 1` would otherwise let a boolean or float id answer
   an integer one), must carry `jsonrpc: "2.0"` framing and an object result,
   and the initialize result must negotiate exactly the protocol version this
   file's `capture-provenance.json` records and name its server. Then the
   initialized notification is sent, and the `tools/call` result's `isError` is
   read as the protocol defines it: the field is OPTIONAL and its absence means
   false, so an omitted `isError` is accepted as success, while a present one
   must be a real boolean and must be false. A numeric `0` and the string
   `"false"` are both rejected outright rather than read for truthiness, since
   neither is a reading of what the server actually said. Anything else aborts
   the run, and nothing is promoted.
5. Asserts the exact expected world (including the published seeded golden
   hashes for schema v1 AND v2), runs the full statediff adapter validation
   over every exported pair (hash recompute, counters, meta, snapshot/JSONL
   cross-checks), and only then promotes ALL sequences at once into this
   directory. A failed capture leaves the committed fixtures untouched.

Sequences:

- `baseline/payment/`: TASK-10 payment release. `erp_release_payment` on
  `INV-2026-00347` as `ap_approver` (seeded pending approval `APR-0001`).
  After-state: one payment `PAY-0001`, invoice paid, approval completed, one
  `PAYMENT_RELEASED` event. Captured after `state_hash`:
  `c9e50af80747b4a77681de98c9aaf44cec96c4809639e03791213e75eb743067`.
- `baseline/hold/`: `erp_place_hold` (`data_mismatch`, `INV-2026-00318`) then
  `erp_release_hold` (`HOLD-0003`), both as `ap_clerk`, with a MID export
  between the two so a baseline with non-empty event history exists (the
  append-only defect needs one). Captured after `state_hash`:
  `686e70070ea1fdbe12b2d64c8d685c2b4e7753e08c90856ea36ea3de8dcacdb8`.

- `baseline/schema2/`: the seeded world under export schema v2 (no
  mutations), so the drifted table and column names are exercised too.
  Reproduces SiloBench's published v2 golden
  `c6b1bcd35a594ddd20d5fdd98310c764db894ace9914b83ec53b4f0101b2cfa4`.

Both v1 `before` snapshots reproduce SiloBench's published seeded golden hash
(`38d60e95a46f0f488c7a594b045df7110b774ed83db6aac35670b4720369a866`).
`capture-provenance.json` records the upstream commit these captures came
from.

## Cross-language hash vectors (`vectors/canonical-vectors.json`)

Generated ONCE by SiloBench's own hash implementation
(`packages/domain/src/hash.ts`, run via `tsx`; the generator script feeds a
set of documents through `canonicalJson` and `sha256Hex` and writes doc,
canonical string, and digest). The set includes non-ASCII keys with a
surrogate-pair case that distinguishes UTF-16 code-unit ordering from naive
code-point ordering, embedded `payload_json` strings, control characters, and
an event-rows array shaped like `audit_hash` input. The Python canonicalizer
is tested against these vectors, so parity does not rest only on the all-ASCII
baseline snapshots.

## Defect variants (`defects/`)

Derived deterministically from the baselines by `tools/inject_defects.py`; see
that file and `docs/design.md` for the defect classes and their consistency
guarantees. Most are semantic: all three hashes and the counter invariants are
recomputed so the artifact stays internally consistent and the defect is
behavioral, not corruption. The exceptions are `broken-hash` and
`broken-counters`, which deliberately break exactly those invariants to exercise
the `error` path (a stored hash that no longer matches its data, a counter that
disagrees with the rows), so they expect an `error` verdict rather than a `fail`.
CI regenerates all variants from the baselines and byte-compares them against the
committed files.
