# StateDiff design

The full semantics behind the README: atoms, coverage, invariants, rule
parameters, the fail-closed list, and the deliberate limitations.

## Diff atoms

The structural diff between two snapshots produces:

- `field_changed {table, pk, column, before, after}`: one atom PER CHANGED
  FIELD of a row present in both snapshots.
- `row_added {table, pk, row}` and `row_removed {table, pk, row}`.
- `event_appended {event}`: one atom per event in the suffix the after
  snapshot appends beyond the before history.

Two tables never enter the generic diff: `events` (represented solely by
event atoms plus the append-only invariant) and `counters` (governed by the
adapter's consistency invariants). `environment` IS diffed: a profile change
mid-run is a real change and must be justified or it fails.

## Effects and coverage

Effects are independent, non-consuming predicates. Every effect evaluates
over the full diff; there is no consumption and no matching order, so the
verdict is a pure function of (scenario, before, after).

- `expected`: each must be satisfied; an unsatisfied expected effect fails.
- `allowed`: never required; exists only to justify atoms. An allowance whose
  own constraint did not hold justified nothing, so its check is
  `not_applicable` rather than `pass`: it never satisfied a predicate, and it
  never blocked the run either.
- `forbidden`: fails when its predicate is satisfied. Only matcher rules
  (`transition`, `count_delta`, `event_exists`) may appear under `allowed` or
  `forbidden`; the invariants (`unchanged`, `idempotent`, `compensated`,
  `correlated`) are valid only under `expected`, enforced at parse time.
- Coverage is conditional on satisfaction: a counting effect justifies the
  atoms it matched ONLY while its own bounds hold. A violated bound covers
  nothing, so its atoms still fall to the sweep.
- The sweep: any atom not covered by a satisfied expected or allowed effect
  is an unexplained change and fails the verdict. This includes appended
  events: an audit event no effect accounts for is a failure, not noise.

## Always-on invariants

- **Append-only audit log**: the before event rows must be a prefix of the
  after event rows, compared as RAW stored rows including the `payload_json`
  string (equality allowed; appending nothing is append-only). Reformatting
  or key-reordering a historical payload therefore fails even though the
  parsed payload object would compare equal. Provenance: REQ-012 / T19.
- **Counters consistency** (adapter, `error` on violation): the `event`
  counter equals the number of events, the `payment` / `hold` / `approval`
  counters equal their tables' row counts, and `mutation_seq` equals the
  metadata value and is at least the maximum event `mutation_seq`. A
  hash-valid snapshot with a stale counter cannot pass.
- **Meta consistency** (adapter, `error`): each snapshot's metadata must
  agree with its own `environment` rows, and the seed must be equal across
  the pair. Profile changes therefore always surface as environment atoms;
  `rules_config: {profile_change: allow}` is the only way to permit them.
- **Schema stability**: a before/after pair with different schema versions is
  an `error`; cross-schema diffing is out of scope in v0.1.
- **Type strictness**: table cells must be SQLite-shaped (string, integer, or
  null; booleans are rejected because Python's `1 == True` would let a
  hash-visible type flip vanish from the diff), snapshot metadata and event
  fields are validated strictly (no string-to-int coercion), event
  `mutation_seq` values must be non-negative and non-decreasing in log order,
  and every comparison that feeds a verdict (payload pins, cross-validation,
  row-to-event correlation, and the open/close pairing in `compensated`)
  separates booleans, integers, and floats. That applies to comparisons made
  by a hash lookup as much as to written-out equality: a dictionary keyed by a
  payload value pairs `1` with `1.0` exactly as `==` does. Python calls all three of `1 ==
  True`, `1 == 1.0` equal and canonical JSON writes them differently, so the
  int/float case matters wherever two independently produced artifacts are
  reconciled: a snapshot payload holding `210000` and a JSONL export holding
  `210000.0` are not the same export, and accepting them as equal would let a
  bundle and a verdict report an event-log cross-check the artifacts do not
  survive.

## Selector syntax

One deliberately tiny syntax everywhere: `table` (name), `key` (primary key
column: value), `field` (column name), `where` / `match` / `scope` (a
conjunction of column: value equality tests). No wildcards, no paths, no
expressions. Every table and column name in a scenario is validated against
the adapter's DDL registry; unknown names are an `error`, never a silent
non-match. Event types are validated against the six known types the same
way. `counters` and `events` are not addressable by table rules at all
(governed by invariants and event rules respectively); referencing them is an
`error` rather than a vacuous pass. Scenario YAML is parsed with the implicit
timestamp resolver removed, so an unquoted `2026-01-03` stays the string the
snapshot stores instead of becoming a `datetime.date` that matches no cell.

Selector VALUES are validated the same way the names are, against what a
validated cell can hold: text, an integer, or null. A boolean, a float, a
list, a mapping, or a date object cannot equal a cell of any artifact that
loads at all, so a scenario carrying one is an `error` rather than a check
that quietly matches nothing. This is the difference between a selector that
did not match and one that could not: `match: {active: true}` against a column
storing 1 counted zero rows and reported a FORBIDDEN effect as passing, so a
boolean spelling mistake disarmed a policy check and reported success while
doing it, and the same mistake in an expected effect failed for a reason its
own detail could not explain. Non-finite numbers are the same story twice
over, since NaN does not equal even itself. The rule applies to every table
selector: `where`, `match`, `scope`, `net_state.where`, and a transition's
`key` plus its `from` / `to` endpoints. `payload_includes` is checked against
the wider JSON domain instead, because event payloads are arbitrary JSON and
booleans, floats, and nested structures are legitimate there; what it rejects
is what is not JSON at all.

Both checks run when the scenario is parsed AND when the rule evaluates,
before any comparison and whether or not any row, atom, or event reaches one,
so a spec assembled or mutated in Python rather than loaded from a file meets
the same refusal. The evaluation-time half is not a repetition of the parse:
a check that only runs at the point of comparison is a check the DATA can
switch off. A forbidden `count_delta` over a table nothing touched, an
`unchanged` over an empty before-table, an `idempotent` or `correlated` over
an empty after-table, and a `compensated` whose `net_state` selects nothing
all reach zero comparisons, and each one then reports the result its own
impossible selector guaranteed rather than the error it is.

## Rule semantics

1. `transition {table, key, field, from?, to?}`: the keyed row exists in both
   snapshots and the field changed; `from`/`to` are each optional and an
   omitted side matches any value. PRESENCE decides that, not the value: an
   explicit `from: null` or `to: null` is a real endpoint (a timestamp column
   starts out null), so it is a pin the transition must satisfy and not an
   omission. Presence is carried by the model's own fields-set rather than by
   a sentinel value, because `from`/`to` accept any value and every in-band
   default collides with a legitimate endpoint. The key must use the table's
   primary key and must name exactly one column. Covers exactly its one field
   atom.
2. `count_delta {table, added?, removed?, match?}`: `added`/`removed` are a
   non-negative exact integer or inclusive `{min, max}` bounds (at least one
   bound, min <= max); at least one of added/removed must be present, since a
   rule that takes both counts and then asserts nothing about either cannot
   fail and so reports a pass it never earned. There
   is no net delta: additions and removals are counted separately so an
   add-plus-remove cannot cancel out. Counts row atoms whose row satisfies
   `match`; covers the counted atoms while the bounds hold. A `match` that
   selects nothing is NOT a violation the way it is for an invariant: the
   bound is an assertion the author wrote down and it is checked either way,
   so a count of zero is a measured result. What protects this rule is the
   selector value domain above, which separates a selector that did not match
   from one that never could.
3. `unchanged {table, where?}`: rows selected in the BEFORE snapshot must
   exist in the after snapshot with every field equal; a selected row removed
   or modified is a violation. Rows entering the subset only in after are
   additions and fall to the sweep. A supplied `where` that selects zero rows
   is itself a violation: it named values that do not exist, so the invariant
   verified nothing, and downstream a pass over an empty selection is
   indistinguishable from a real one. What decides is whether the key was
   supplied, not what it holds, so an explicit `where: {}` takes that path
   too (it still means every row, as a conjunction of zero conditions must).
   Only the bare whole-table form makes no such claim, so freezing an empty
   table is a pass. Invariant, covers nothing.
4. `event_exists {type, count, entity_type?, entity_id?, actor?, system?,
   payload_includes?}`: bounded count of matching events in the appended
   suffix. The identity filters are top-level event columns; this is how
   acting identity is enforced. Covers matched events while the bounds hold.
5. `idempotent {mode: unique_effect, table, key_fields, scope?}`: within the
   scoped after-table rows, no two rows share the same key_fields tuple. The
   single-run form of idempotency: a retried operation must not duplicate its
   effect. A supplied `scope` that selects zero rows is a violation, on the
   same rule as `unchanged.where` and `correlated.match`: uniqueness over an
   empty set holds in every world that ever existed, so it establishes nothing
   about this one while a consumer reading `passed` sees an idempotency
   guarantee. Presence of the key decides, not its contents, so an explicit
   `scope: {}` takes that path too; omitting it is the whole-table form and
   makes no claim about which rows exist, so an empty table passes there.
   Invariant, covers nothing.
6. `compensated {open_event: {type, payload_key}, close_event: {type,
   payload_key}, net_state: {table, where, expect_count}}`: over the suffix
   only (seeded history predates the event log by the upstream contract),
   pair openings to closings one-to-one on equal payload-key values, FIFO,
   closer at or after its opener. Equal means TYPE-exact, the same way every
   other comparison feeding a verdict does: payloads are arbitrary JSON, so an
   opener keyed by `1` and a closer keyed by `1.0` are two events that do not
   agree about what was opened, and pairing them certifies a compensation
   neither event describes. An opener without a closer, a closer
   without an opener, or a missing payload key is a violation; then the
   net-state row count must equal `expect_count`. Covers the paired events.
   Pairs events to each other only; both ends can agree on a key that names
   no row, which is what rule 7 is for. `net_state.where` is a counted
   assertion like rule 2 and not an invariant selector: it is REQUIRED and it
   is paired with a required `expect_count`, so an empty selection is exactly
   what `expect_count: 0` asserts. The shipped hold scenario is that case
   (`active: 1` after the release must select nothing), and failing an empty
   selection there would make "none of these remain" inexpressible.
7. `correlated {table, match?, event: {type, payload_key}, count?}`: the rows
   the selector names in the AFTER snapshot and the suffix events that
   reference them are the same entities. Forward, each selected row must be
   named by `count` events of that type (default exactly one) whose
   `payload_key` equals the row's primary key; reverse, no suffix event of
   that type may carry a value naming no row at all. Every other rule checks
   rows and events in separate universes, so a scenario could require a
   payment row and a PAYMENT_RELEASED event and never state that the event
   audits that row, and an unaudited payment satisfied it. The reverse
   direction is checked against the whole table rather than the selected
   subset, so a run that legitimately touches other rows is not flagged for
   them. A selector matching no rows is a violation, with no whole-table
   exemption: a correspondence with nothing on one side is not one. Missing,
   null, and non-scalar payload values are named violations, shared with rule
   6 so both refuse the same values for the same reason. Invariant, covers
   nothing: the rows and events still need their own expected effects, so a
   declared correlation can never launder a change past the sweep. The human
   report's correlation block is rendered from this rule's evidence rather
   than re-derived, so the report and the verdict cannot disagree.

## Verdict

`statediff.verdict.v1`: scenario id, status (`pass` | `fail` | `error`),
per-check outcomes with evidence atoms, the unexplained-change list, artifact
fingerprints (schema version, state hashes, event counts,
`before_events_cross_checked` / `after_events_cross_checked`), and provenance
passthrough. Reserved check ids: `append_only`, `unexplained`, `artifact`,
`verdict` (scenario effects may not use them).

A check status is `pass`, `fail`, or `not_applicable`; only an allowed effect
that did not fire produces the third. The gate projection's `passed` answers
"did this block the run", the only question a two-state boolean can answer,
and a `not_applicable` check answers it in neither direction, so it is NOT
PROJECTED AT ALL. Allowed effects are never required, so `false` would fail a
run over an effect that was under no obligation to happen; `true` would hand
the consumer a named check that verified nothing, and the consuming gate
counts merged state checks toward how much a run actually asserted, so a
projected non-assertion inflates that count. The one thing this projection
must never do is overstate what was checked, and omission is the only reading
of an unfired allowance that cannot. It is not silent omission: the
always-present `statediff:verdict` entry names every check left out, and the
verdict JSON keeps the full three-state list for consumers that want it.

The event logs are optional inputs, so the event counts alone say nothing
about whether a snapshot was ever reconciled with an independent export. The
two cross-check flags record whether that reconciliation actually ran, per
side, and are reported in the verdict, in the human report, and in a run
bundle's manifest. `check --bundle` verifies the manifest's flags against the
pair it just reloaded, so a manifest cannot claim a cross-check whose event
logs are not in the bundle to run it.

Fail-closed `error` enumeration: unparseable artifact, wrong format version,
duplicate JSON object key anywhere statediff parses JSON (artifact, embedded
event payload, event log, bundle manifest, stored verdict: last-wins parsing
would hide a rewrite from hashes computed over the parsed structure),
unexpected table set, row columns diverging from the DDL, null or duplicate
primary keys, embedded hash mismatch on recompute, counters inconsistency,
meta/environment disagreement, seed or schema mismatch across the pair,
backwards mutation_seq, a value outside the JSON domain (NaN, an infinity, or
a decoded lone surrogate) anywhere statediff parses artifact JSON, embedded
event payload and standalone event log alike, event-log cross-validation
mismatch (including a payload number whose type differs between the two
exports), unknown rule, unknown table/column/event type in a scenario, a
selector value outside the cell domain or a payload pin outside the JSON
domain, invariant rule outside expected, malformed bounds, a `count_delta`
with neither bound, a transition key that is not exactly one column, a
scenario that declares no effects at all, duplicate or reserved effect ids,
and an unknown effect list (reported even when the document's keys are of
mixed types, which a naive sort could not order). Every one of these fails a
downstream gate exactly like `fail`.

A `Scenario` assembled or mutated in Python rather than loaded from a file
reaches the same enumeration, and every layer that can be bypassed is repeated
by one that cannot. `parse_scenario` is what refuses an unknown rule name and
pairs each effect with the spec model of its own rule, so the engine repeats
both checks at dispatch: otherwise the exported API answers a library caller
with a KeyError traceback from outside `evaluate`'s handler, where every other
unusable scenario answers with an `error` verdict. Each rule likewise repeats
its own spec's validators when it evaluates, because `model_construct`,
`model_copy`, and plain attribute assignment all skip a model validator, and
the refusal inside the selector only sees the values a row happens to reach it
with. The empty effect list is the same shape one level up: the model refuses
it and the engine refuses it again, since a scenario that declares nothing
leaves only the append-only prefix and the unexplained sweep, both of which an
unchanged pair satisfies, and `pass` would be the answer to a question nobody
asked.

The gate projection (`gate.py`) emits `{name, passed, detail}` entries per
check plus an always-present `statediff:verdict` entry, so no consumer can
filter its way into reading a failing verdict as green.

## Cross-language hash parity

`canonical.py` reimplements the upstream canonical JSON: object keys sorted
by UTF-16 code units (Python: sort by the UTF-16BE encoding of the key), no
whitespace, UTF-8, sha256. Every artifact load recomputes `world_hash`,
`audit_hash`, and `state_hash` and compares them to the embedded values.
Parity is pinned two ways: the committed baseline reproduces the upstream
system's published seeded state hash, and committed cross-language vectors
(generated once by the upstream TypeScript implementation) cover non-ASCII
keys including a surrogate-pair ordering case that distinguishes UTF-16
code-unit order from naive code-point order, embedded JSON strings, and an
event-rows array shaped like the audit-hash input.

## Fixture strategy and honesty

Baselines are real captures through the upstream system's MCP stdio servers
(validated JSON-RPC conversation, post-capture assertions; see
`fixtures/README.md`). Defect variants are derived by a deterministic
injector that keeps artifacts internally consistent, so defects are semantic.
The hold baseline exports a MID state (after the hold placement) because the
append-only defect needs a before with non-empty history: tampering over an
empty history is a valid extension of an empty prefix and would be
undetectable by a prefix check.

The detection matrix evaluates every defect class under every one of 100
injector seeds (600 defective evaluations), asserts the intended check id
fires for every case, and asserts both clean pairs stay fully green each
round. CI regenerates the committed variants and byte-compares them in both
directions (stale committed files AND uncommitted generator outputs are
drift), with platform-independent LF bytes and a `.gitattributes` guard
against newline conversion. Baseline captures record the upstream commit id
in `capture-provenance.json` and refuse a dirty upstream tree, and each
sequence is promoted from staging only after full adapter validation of every
exported pair, so a failed capture never overwrites committed baselines.

## Deliberate limitations (v0.1)

1. The timeout-after-commit regression is modeled in derived artifacts; no
   live retry executes and no agent is in the loop yet. The flagship defect's
   run context is labeled modeled.
2. The upstream world has exactly one seed, so the zero-false-positive leg is
   a determinism check over fixed corrected artifacts.
3. `idempotent` has no replay-comparison mode yet (needs a two-after input
   shape and a definition of same-intended-operation).
4. Cross-schema diffing is out of scope.
5. Gate registration is a consumer-side change: this repo ships the
   projection shape, not the wiring into any specific runner.
6. Lone UTF-16 surrogates in artifact strings are rejected (an `error`), not
   canonicalized: upstream v1 artifacts cannot contain them, and matching the
   reference implementation's encoder behavior for invalid Unicode would add
   complexity for data that cannot legitimately exist.
7. Hash COMPOSITION for newly derived states is computed by the same code
   that validates it; independence rests on the cross-language vectors (which
   pin serialization, including non-ASCII ordering) and on the five real
   baselines whose upstream-produced hashes the same code reproduces.
