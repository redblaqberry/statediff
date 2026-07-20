# StateDiff

State oracle for agent evaluation: did the world change correctly?

Answer grading and trajectory grading both pass an agent that double-charged a vendor and reported that everything succeeded. The answer sounded right and the tool calls looked right; the damage is only visible in the world. StateDiff grades exactly that: it takes a before snapshot, an after snapshot, and the append-only audit log of a system an agent worked on, evaluates them against a scenario of expected, allowed, and forbidden effects, and returns a deterministic verdict with evidence.

Built against [SiloBench](https://github.com/redblaqberry/silobench), a deterministic synthetic enterprise whose `snapshot.v1` and `event-log.v1` export formats this tool consumes. All fixture data is synthetic and models fictional companies.

## The model

StateDiff computes a structural diff between the two snapshots: one atom per changed field, added row, removed row, and appended audit event. Then it demands justification:

- **expected** effects must all hold, or the verdict fails.
- **allowed** effects are never required; they only justify changes.
- **forbidden** effects must match nothing.
- **Every atom must be justified** by an expected or allowed effect. An unaccounted-for change fails the verdict, so the default is deny, not ignore.
- **The audit log must be append-only**: the before event history must be a prefix of the after history. Rewritten history fails.
- Artifacts are validated before anything else: all three embedded hashes are recomputed, internal counters must be consistent with the data, and metadata must agree with the stored profile. Anything inconsistent is an `error` verdict, which fails downstream gates exactly like `fail`.

The verdict is `pass`, `fail`, or `error`, with CLI exit codes 0, 1, and 2. It is a pure function of the scenario, the before snapshot, and the after snapshot. The append-only check runs on the event history embedded in the snapshots, with or without the optional event logs. Supplying those logs cross-validates them against that embedded history and records the result as cross-check flags in the verdict, so a log that disagrees with its snapshot can turn a `pass` into an `error` but never the reverse, and the snapshot-only verdict is stable and reproducible on its own.

## Quickstart

Requires Python >= 3.10 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest          # 277 tests incl. the 600-case defect detection matrix

# The correct run passes: every change the approver's payment release made is
# expected, and nothing else changed.
uv run statediff check --scenario scenarios/payment-release.yaml \
  --before fixtures/baseline/payment/before-snapshot.json \
  --after  fixtures/baseline/payment/after-snapshot.json

# The planted timeout-after-commit regression fails: two payment rows, one
# audit event. Exit code 1.
uv run statediff check --scenario scenarios/payment-release.yaml \
  --before fixtures/baseline/payment/before-snapshot.json \
  --after  fixtures/defects/duplicate-payment/after-snapshot.json

# Machine-readable verdict (stdout is pure JSON; the human report goes to stderr):
uv run statediff check --scenario scenarios/payment-release.yaml \
  --before fixtures/baseline/payment/before-snapshot.json \
  --after  fixtures/defects/duplicate-payment/after-snapshot.json --json > verdict.json

uv run statediff explain verdict.json
```

The flagship failure reads like this:

```
SD-PAY-01: Approver payment release changes exactly what policy permits
VERDICT: FAIL
  [FAIL] one-payment (count_delta, expected): payments matching {...}: added 2 (want exactly 1)
  [PASS] audit-payment (event_exists, expected): 1 new PAYMENT_RELEASED event(s) ... (want exactly 1)
  [FAIL] payment-audited (correlated, expected): payments matching {...} <-> PAYMENT_RELEASED.payment_id: 1/2 row(s) not audited exactly 1 time(s), 0/1 event(s) not resolving to a row
  [FAIL] at-most-one-payment-per-invoice (idempotent, expected): payments: 2 rows share 1 invoice_id value(s); the effect was duplicated
  [FAIL] no-second-payment (count_delta, forbidden): payments: added 2 (want >= 2)
  ...
Row/event correlation:
  payments[PAY-0001] -> PAYMENT_RELEASED: audited by EVT-0001
  payments[PAY-0002] -> PAYMENT_RELEASED: NO AUDIT EVENT EXISTS
```

Note which check passes there. `audit-payment` asks whether the right kind of event was written and the answer is yes: one `PAYMENT_RELEASED` event exists with the right actor, system, and payload. Only `payment-audited` asks whether that event refers to the rows that actually appeared. An oracle that checks the two independently can be satisfied by a payment nobody audited, so the correlation is a real check whose evidence the report renders, not a display join computed beside the verdict.

The agent that produced this world reported "payment released successfully". Its answer would pass an answer check; its tool calls would pass a trajectory check. The world says otherwise.

## The seven rules

| Rule | Question it answers | Key parameters |
|---|---|---|
| `transition` | Did this row's field go exactly from A to B? | `table`, `key` (one column), `field`, optional `from`/`to`. Presence decides, not the value: an omitted side matches anything, an explicit `from: null` is a real pin |
| `count_delta` | How many rows appeared or disappeared, and which? | `table`, `added`/`removed` (exact or `{min,max}`), `match` row selector |
| `unchanged` | Did these rows stay untouched? | `table`, optional `where` selector over the before state |
| `event_exists` | Was this audited, by the right identity? | `type`, `count`, `actor`, `system`, `entity_id`, `payload_includes` |
| `correlated` | Does the audit event actually name this row? | `table`, optional `match`, `event: {type, payload_key}`, optional `count` (default exactly 1) |
| `idempotent` | Was the effect duplicated by a retry? | `mode: unique_effect`, `table`, `key_fields`, optional `scope` |
| `compensated` | Was every opening action closed again? | `open_event`/`close_event` paired on a payload key, plus a `net_state` condition |

`correlated` checks both directions: every selected row must be named by the expected number of new events, and no new event of that type may name a value that resolves to no row. The reverse side is checked against the whole table rather than the selected subset, so a run that legitimately touches other rows is never falsely flagged.

`unchanged`, `correlated`, `idempotent`, and `compensated` are invariants and only valid under `expected`. Counting effects justify the atoms they matched only while their own bounds hold, so a violated bound never launders the rows it counted. Selectors are deliberately tiny: a column-to-value equality conjunction, nothing more.

Selector values are checked against what they can possibly match, when the scenario is parsed and again when each rule evaluates, before any comparison and whether or not a single row reaches one. A table-cell selector (`where`, `match`, `scope`, a transition `key`) is compared against a SQLite cell, which holds only text, an integer, or null, so a boolean, a float, or a date there is an `error` rather than a check that quietly matches nothing. Event payload pins (`payload_includes`) are matched against JSON, where finite numbers and booleans are legitimate, so those are allowed and only a non-finite number is refused. A string carrying a lone UTF-16 surrogate is refused in either place, because artifacts are UTF-8 files and cannot hold one.

Validating twice is not belt and braces. A check that runs only at the point of comparison is a check the data can switch off: a forbidden effect over a table with no candidate rows never reaches a comparison, so it reported exactly the pass its own impossible selector guaranteed. `match: {active: true}` against a column storing `1` counted zero rows and reported a forbidden effect as passing, which is a policy check disarmed by a spelling mistake. The same parse-and-evaluate refusal covers everything a Python-mutated spec could otherwise switch off after construction: a count bound no set of matches can have (negative, or min above max, or neither present), an event's identity filters, an idempotency key naming no fields, and a compensated effect pairing an event type with itself. Pydantic validates on construction, not assignment, so each rule re-asserts its own spec when it runs, and a scenario built or mutated in Python gets the identical refusal to one loaded from a file.

The three universally quantified invariants fail when a supplied selector matches nothing: `unchanged.where`, `correlated.match`, and `idempotent.scope`. Each asserts something about every member of a selected population, which is vacuously true over an empty one, and a machine consumer reading the gate projection has no way to tell that apart from a real pass. What decides is whether the key was supplied, not whether it has contents, so an explicit `where: {}` fails over an empty table too. The bare whole-table forms still pass over an empty table, because freezing a table before any row exists is a legitimate blanket guard and its emptiness is a fact about the world rather than a claim by the author.

The counting rules are the deliberate counterpart. `count_delta` and `compensated`'s `net_state` declare a number the author expects, so an empty selection is a measured result rather than a vacuous pass: `added: 0` and `expect_count: 0` are exactly how "none of these appeared" and "none of these remain" are expressed, and failing them would make those inexpressible.

`compensated`'s `net_state` is a counting measure in that sense, but its pairing side is an invariant: a suffix with no opening event of the declared type fails, because "every opener was closed" is vacuously true over zero openers, and a `compensated` effect asserts that compensation actually happened rather than that nothing needing it occurred.

Scenario YAML is parsed with the implicit timestamp resolver removed, so an unquoted `2026-01-03` in a selector stays the string the snapshot actually holds instead of becoming a `datetime.date` that matches nothing. Duplicate mapping keys are rejected in scenarios, and duplicate keys are rejected in every artifact JSON as well: because hashes are recomputed over the parsed structure, a last-value-wins parse would let a file show one value to a reader and feed another to the rules while still verifying.

Scenarios carry provenance: requirement ids and discovery-transcript turns (`REQ-007`, `T23`) flow from the upstream deployment contract into every verdict, so a failing check traces back to the customer statement that made it a rule. The contract these ids resolve against is the approved output of [DiscoverySpec](https://github.com/redblaqberry/discoveryspec).

## Fixtures: real captures, modeled defects

The `fixtures/baseline/` pairs are REAL: captured from an unmodified SiloBench checkout through its actual MCP stdio servers, with a validated JSON-RPC conversation, full adapter-grade validation, and all-or-nothing promotion (procedure in `fixtures/README.md`; the upstream commit id is recorded in `capture-provenance.json`, and captures from a dirty upstream tree abort). The seeded snapshots reproduce SiloBench's published golden state hashes for BOTH export schema versions, recomputed here in Python: the canonical JSON and sha256 implementations are byte-compatible across languages, pinned by committed cross-language test vectors that include surrogate-pair key ordering.

The `fixtures/defects/` variants are DERIVED: `tools/inject_defects.py` deterministically plants six defect classes (duplicate payment, lost audit write, invalid transition, unexplained mutation, rewritten history, uncompensated hold) while keeping every artifact internally consistent: hashes recomputed, counters coherent, so each defect is semantic, never file corruption. SiloBench itself cannot produce these states (its ERP enforces exactly-one-payment); they model a buggy downstream integration, which is precisely the class of failure a state oracle exists to catch. Two additional variants are deliberately inconsistent to exercise the `error` path. CI regenerates all variants and byte-compares them against the committed files.

The detection matrix runs every defect class under every one of 100 injector seeds (600 defective evaluations): each must fail on its intended check, and both clean pairs must stay fully green in every round.

## Integration

`statediff check --gate` prints check results in the shape deterministic-check runners consume: `{name, passed, detail}` with colon-namespaced names (`statediff:one-payment`, `statediff:append_only`). A verdict-level entry (`statediff:verdict`) is always included, so a failing or erroring verdict can never be projected as an all-green list. Check status is three-state internally (`pass`, `fail`, `not_applicable`, the last arising only from an allowed effect that never fired). `passed` answers "did this block the run", which a check that never fired answers in neither direction, so it is not projected at all: `false` would fail a run over an effect nothing required, and `true` would inflate the assertion count a consuming gate derives from merged checks. The always-present `statediff:verdict` entry names every omitted check, and a consumer that needs the distinction reads the verdict JSON. [agent-eval-gate](https://github.com/redblaqberry/agent-eval-gate) merges these directly: write each scenario's output to `<dir>/<scenario_id>.json` and pass `--state-checks <dir>` to its run command. The verdict JSON itself (`statediff.verdict.v1`) carries per-check evidence, the unexplained-change list, artifact fingerprints, and provenance.

`statediff capture` validates an artifact pair (formats, hashes, counters) and stores it as a named run bundle that `check --bundle` consumes; the bundle manifest's fingerprints are enforced on use, so artifacts swapped after capture are rejected. The snapshot/event-log cross-check runs only for the sides whose JSONL log was supplied, so the bundle manifest, the verdict's `artifacts` block, and the human report each record `before_events_cross_checked` and `after_events_cross_checked` explicitly. Where it does run it is type-exact down into the payload, so a snapshot holding `210000` and a JSONL holding `210000.0` are rejected rather than reconciled: the point of comparing two independently produced artifacts is that they agree exactly. A bundle that was never cross-checked cannot present as one that was, and a manifest claiming a cross-check whose logs are absent is rejected. `check --bundle` refuses to be combined with `--before`, `--after`, or the event-log options, because a bundle is the whole fingerprinted input set and an artifact supplied from outside it would be covered by no fingerprint. Every failure mode, including unusable scenarios and broken bundles, still produces machine-readable output rather than a traceback: the full `statediff.verdict.v1` error object under `--json`, and the gate-check array (carrying the failing `statediff:verdict` entry) under `--gate`, including for an artifact whose own fields cannot be UTF-8 encoded.

## Project structure

```
src/statediff/
  canonical.py   canonical JSON + sha256, byte-compatible with the source system
  adapter.py     artifact loading and every fail-closed validation invariant
  diff.py        structural diff: field, row, and event atoms
  scenario.py    scenario schema and parsing (statediff.scenario.v1)
  rules/         the seven rule evaluators
  engine.py      justification sweep, append-only invariant, verdict assembly
  evidence.py    human rendering, driven by check evidence so the report
                 and the verdict cannot disagree
  gate.py        gate-style check projection
  cli.py         capture / check / explain
scenarios/       payment-release.yaml (flagship), hold-compensation.yaml
fixtures/        baseline captures, derived defects, cross-language hash vectors
tools/           capture driver and defect injector
docs/design.md   the full semantics: atoms, coverage, invariants, limitations
```

## Deliberate limitations (v0.1)

- The timeout-after-commit regression is modeled in derived artifacts, not executed live; there is no agent in the loop yet. The run context shipped with the flagship defect is labeled as modeled.
- One world seed exists upstream by design, so the zero-false-positive leg of the matrix is a determinism check over fixed corrected artifacts, not 100 independent corrected executions.
- `idempotent` ships the single-run `unique_effect` mode; a replay-comparison mode needs a two-after input shape and is deferred.
- Cross-schema diffing (a v1 before against a v2 after) is out of scope and errors.
- A scenario that declares no effects, and a `count_delta` carrying neither an `added` nor a `removed` bound, are both an `error`. A rule that asserts nothing cannot pass, and neither can a scenario made entirely of them.
- The gate-side wiring lives in the consumer (agent-eval-gate's `--state-checks` flag); this repo ships only the projection shape.

## License

[MIT](LICENSE)
