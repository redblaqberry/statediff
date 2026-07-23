"""Scenario files (`statediff.scenario.v1`): expected / allowed / forbidden
effects over a before/after artifact pair. Parsing fails closed: unknown rule
names, unknown keys, conflicting count forms, duplicate effect ids, and
invariant rules outside `expected` are all hard errors."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Annotated, Any, Literal, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class ScenarioError(ValueError):
    """The scenario itself is invalid; evaluation must return `error`."""


class Bounds(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    min: int | None = Field(default=None, ge=0)
    max: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _check(self) -> "Bounds":
        if self.min is None and self.max is None:
            raise ValueError("bounds need at least one of min/max")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("bounds min must be <= max")
        return self


# strict + ge=0: "-1", True, and 1.0 are all rejected, so a forbidden effect
# can never be given an exact count that is impossible to fire.
ExactCount = Annotated[int, Field(strict=True, ge=0)]
CountSpec = Union[ExactCount, Bounds]


def count_satisfied(count: int, spec: CountSpec) -> bool:
    if isinstance(spec, int):
        return count == spec
    if spec.min is not None and count < spec.min:
        return False
    if spec.max is not None and count > spec.max:
        return False
    return True


def count_describe(spec: CountSpec) -> str:
    if isinstance(spec, int):
        return f"exactly {spec}"
    parts = []
    if spec.min is not None:
        parts.append(f">= {spec.min}")
    if spec.max is not None:
        parts.append(f"<= {spec.max}")
    return " and ".join(parts)


def _require_count_value(where: str, value: Any) -> None:
    # Booleans first: `isinstance(True, int)` holds in Python, so `count == True`
    # would quietly read as "exactly 1", which is not what an author who wrote
    # `true` asked for.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScenarioError(
            f"{where} is {value!r} ({type(value).__name__}), not a whole number; "
            "a count of matches is a non-negative integer"
        )
    if value < 0:
        raise ScenarioError(
            f"{where} is {value}; no set of matches can have a negative size, "
            "so this bound can never be met"
        )


def require_count_spec(where: str, spec: Any) -> None:
    """Refuse a count no set of matches can ever have.

    `ExactCount` and `Bounds` enforce this when a model is BUILT, and nothing
    downstream can stand in for it. `count_satisfied` answers "the count is not
    the required one", which reads identically whether the run fell short or
    the requirement was unreachable to begin with, so a forbidden effect pinned
    to a negative exact count, or to bounds whose min exceeds their max,
    counted its matches honestly and then reported PASS because no count could
    have satisfied it. A count that is not an integer or `Bounds` at all is
    worse than unreachable: `count_satisfied` reads `.min` off it and raises
    AttributeError from OUTSIDE the handler that turns every other unusable
    scenario into an `error` verdict.
    """
    if isinstance(spec, Bounds):
        for name, bound in (("min", spec.min), ("max", spec.max)):
            if bound is not None:
                _require_count_value(f"{where}.{name}", bound)
        if spec.min is None and spec.max is None:
            raise ScenarioError(
                f"{where} carries neither a min nor a max, so it bounds nothing and "
                "every count satisfies it"
            )
        # `{min: 0}` with no max is the same assertion wearing a number: every
        # possible count is at least zero, so it can never fail, it counts as an
        # expected effect that "asserted" something, and whatever it matches it
        # covers, walking appended events past the unexplained sweep.
        if spec.min == 0 and spec.max is None:
            raise ScenarioError(
                f"{where} wants at least zero matches with no upper bound; every "
                "count satisfies that, so it asserts nothing"
            )
        if spec.min is not None and spec.max is not None and spec.min > spec.max:
            raise ScenarioError(
                f"{where} wants at least {spec.min} and at most {spec.max}, "
                "which no count can satisfy"
            )
        return
    _require_count_value(where, spec)


# The one value EVERY selector domain below refuses, though they agree on
# nothing else: a snapshot cell holds text, an integer, or null, an event
# payload holds arbitrary JSON, and an event's identity columns hold text. All
# three are read out of a UTF-8 artifact, UTF-8 cannot encode a lone UTF-16
# surrogate at all, and the adapter refuses an artifact carrying one before a
# single row or payload is read. So no string in an artifact that LOADS holds
# one, in any of the three domains, and comparing against one can only ever
# answer "these differ", which is indistinguishable from a real non-match: an
# expected effect fails for a reason its own detail cannot explain, and a
# FORBIDDEN one reports PASS because it can never fire, disarming a policy
# check while reporting success.
def require_encodable_text(where: str, value: Any) -> None:
    """Refuse a string no artifact statediff loads can carry."""
    if not isinstance(value, str):
        return
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ScenarioError(
            f"{where} is {value!r}, which carries a lone UTF-16 surrogate; artifact strings "
            "are UTF-8 and cannot hold one, so this can never match anything an artifact carries"
        ) from None


# What a validated snapshot cell can hold, from the adapter's row check: text,
# an integer, or null. Booleans are excluded there because SQLite has no boolean
# type and Python's `1 == True` would let a hash-visible type flip read as
# equal. A selector value outside that set cannot equal a cell of ANY artifact
# that loads at all, so it is refused when the scenario is parsed instead of
# being compared row by row. Comparison can only ever answer "these differ",
# which is indistinguishable from a real non-match: an expected effect built on
# such a value fails for a reason its own detail cannot explain, and a FORBIDDEN
# one reports PASS because it can never fire, so `active: true` against a column
# holding 1 disarms a policy check and reports success while doing it.
_CELL_DOMAIN = "a snapshot cell holds only text, an integer, or null"


def _uncomparable_cell_value(value: Any) -> str | None:
    """Why `value`'s TYPE can never equal a snapshot cell, or None if it can.

    Only the type question; whether the artifact can carry the text at all is
    the shared encodability check above, and `require_cell_value` asks both.
    """
    if value is None or isinstance(value, str):
        return None
    if isinstance(value, bool):
        # Before the int case: `isinstance(True, int)` is true in Python, which
        # is the whole reason a boolean selector looked comparable.
        return f"the boolean {value!r} (a flag column stores the integer 1 or 0)"
    if isinstance(value, int):
        return None
    if isinstance(value, float):
        # Covers .inf and .nan, which additionally equal nothing at all.
        return f"the float {value!r} (cells hold integers, and 1.0 is not the integer 1)"
    return f"a {type(value).__name__} ({value!r})"


def require_cell_value(where: str, value: Any) -> None:
    require_encodable_text(where, value)
    problem = _uncomparable_cell_value(value)
    if problem is not None:
        raise ScenarioError(
            f"{where} is {problem}; {_CELL_DOMAIN}, so this selector can never match a row"
        )


def require_cell_values(field: str, values: dict[str, Any] | None) -> None:
    """Refuse every selector value a snapshot cell cannot hold.

    Shared by the scenario models and by the runtime selector in `rules.base`,
    so a spec built in Python meets the same refusal a parsed one does.
    """
    if values is None:
        return
    # Presence, not truthiness, and the SHAPE is checked before the contents. A
    # selector mutated in Python to a list rather than a mapping is not empty,
    # it is unusable: `(values or {})` used to coerce it to `{}` and validate
    # nothing, and reading `.keys()` off it in the rule then raised
    # AttributeError from OUTSIDE the handler that turns every other unusable
    # scenario into an `error` verdict.
    if not isinstance(values, dict):
        raise ScenarioError(
            f"{field} is a {type(values).__name__} ({values!r}), not a mapping of column to "
            "value; a selector that is not a mapping can never match a row"
        )
    for column, value in values.items():
        require_cell_value(f"{field}.{column}", value)


# A payload pin is finite JSON, but a YAML anchor can alias a node back into
# itself, so `payload_includes: {k: &a [*a]}` builds a list that contains
# itself. Walking it with no guard recursed until the interpreter stack gave
# out, and a RecursionError is not a `ScenarioError`, so `check --json` died
# with a traceback where every failure mode is promised a machine-readable
# error verdict. The ancestor set catches a true cycle (a node that encloses
# itself along the current path); the depth cap catches a merely pathological
# nesting before the stack does. A shared alias that is NOT a cycle, the same
# node aliased by two siblings, stays legal, because each branch carries only
# its own ancestors.
_MAX_PIN_DEPTH = 200


def _require_json_value(
    where: str, value: Any, _ancestors: frozenset[int] = frozenset(), _depth: int = 0
) -> None:
    if _depth > _MAX_PIN_DEPTH:
        raise ScenarioError(
            f"{where} nests deeper than {_MAX_PIN_DEPTH} levels; a payload pin that deep is a "
            "malformed or cyclic alias structure, not a value any event payload can carry"
        )
    if isinstance(value, str):
        require_encodable_text(where, value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ScenarioError(
                f"{where} is {value!r}, which is not a JSON value and cannot appear in a "
                "validated event payload, so this pin can never match"
            )
    elif isinstance(value, (dict, list)):
        if id(value) in _ancestors:
            raise ScenarioError(
                f"{where} refers back into a container that already encloses it; a cyclic alias "
                "structure is not a value any event payload can carry"
            )
        ancestors = _ancestors | {id(value)}
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ScenarioError(f"{where} has the non-string object key {key!r}; JSON keys are text")
                # The KEY as well as its value: `event_matches` first asks whether
                # the key is PRESENT in the payload, so a key no payload can carry
                # answers no for every event there is, before any value is compared.
                require_encodable_text(f"the object key in {where}", key)
                _require_json_value(f"{where}.{key}", item, ancestors, _depth + 1)
        else:
            for index, item in enumerate(value):
                _require_json_value(f"{where}[{index}]", item, ancestors, _depth + 1)
    elif not (value is None or isinstance(value, (str, int, bool))):
        raise ScenarioError(
            f"{where} is a {type(value).__name__} ({value!r}), which is not a JSON value, "
            "so this pin can never match an event payload"
        )


def require_payload_values(field: str, values: dict[str, Any] | None) -> None:
    """Refuse payload pins that no event payload can carry.

    Payloads are arbitrary JSON, so this domain is deliberately wider than a
    table cell's: booleans, floats, and nested structures are all legitimate
    here. What it excludes is what is not JSON at all. NaN and the infinities
    are why it exists: the artifact loader refuses them, and NaN does not equal
    even itself, so pinning one reads like a constraint while matching nothing.
    """
    if values is None:
        return
    # Presence, not truthiness, and the pins must be a mapping. An empty `{}` is
    # a real zero-pin value the author wrote and is validated as one, but a spec
    # mutated to `payload_includes=[]` is the wrong shape, not empty: `values or
    # {}` used to coerce both `[]` and `{}` to `{}`, so the list skipped this
    # check and `event_matches` then skipped its pin loop, passing an expected
    # effect while its detail still claimed the pins were checked.
    if not isinstance(values, dict):
        raise ScenarioError(
            f"{field} is a {type(values).__name__} ({values!r}), not a mapping of key to value; "
            "payload pins are an object, so this can never match an event payload"
        )
    # Routed through the object case rather than looped over here, so a pin's
    # own top-level KEYS meet the same refusal its values and its nested keys do.
    _require_json_value(field, values)


def require_event_filters(spec: "EventExistsSpec") -> None:
    """Refuse an event identity filter no validated event column can equal.

    `type` is absent on purpose: it is checked against the six known event
    types, which refuses this and more, and `table` gets the same treatment
    from its own registry. The four filters below have NO registry to be checked
    against, so this is the only thing between one no event can hold and a
    forbidden effect that counts zero matches and calls that a pass. An event's
    identity columns are text (the EventRecord model types them so), which is
    two refusals in one: a non-string filter equals none of them, and a string
    carrying a lone surrogate reaches no event either. The model states both
    when it is built, and `model_construct`, `model_copy`, and plain attribute
    assignment all skip that.
    """
    for field in ("entity_type", "entity_id", "actor", "system"):
        value = getattr(spec, field)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ScenarioError(
                f"{field} is {value!r} ({type(value).__name__}); an event's {field} is text, "
                "so this filter can never match an event"
            )
        require_encodable_text(field, value)


def require_event_selector(where: str, selector: "EventSelector") -> None:
    """Refuse an event selector's payload key no event payload can carry.

    `type` is left to `require_event_type`'s registry, exactly as the identity
    filters leave it: an unknown type is refused there and a known one is ASCII.
    `payload_key` has NO registry. It is looked up in an event payload
    (`key in event.payload`), so a lone surrogate reaches no key any UTF-8
    artifact can carry, and a `compensated` or `correlated` effect whose events
    of the type are absent then reports its vacuous pass ("0 ... each closed")
    over a key nothing can hold. The model states this when it is built;
    `model_construct`, `model_copy`, and plain attribute assignment skip it.
    """
    key = selector.payload_key
    # A JSON object key is a string, and `require_encodable_text` only inspects
    # strings, so it silently accepted a `payload_key` mutated to 123 or None. A
    # non-string key can match no payload, so the same absent-events vacuous pass
    # this guard exists to stop reopens; refuse the wrong type before encodability.
    if not isinstance(key, str):
        raise ScenarioError(
            f"{where}.payload_key is {key!r} ({type(key).__name__}); a payload key is looked up "
            "in an event payload whose keys are strings, so a non-string key can never match"
        )
    require_encodable_text(f"{where}.payload_key", key)


class _EffectBase(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    id: str = Field(min_length=1)


class TransitionSpec(_EffectBase):
    table: str
    key: dict[str, Any]
    field: str
    # Which endpoints were supplied is carried by the model's own fields-set,
    # not by a sentinel VALUE. `from`/`to` are `Any`, so no in-band default can
    # mean "omitted" without colliding with a real endpoint, and the `object()`
    # sentinel that used to mean it did not survive leaving this model:
    # `model_copy(deep=True)` deep-copies it into a NEW object that is no
    # longer `is` the original, so every omitted endpoint came back looking
    # supplied and pinned to a value nothing can equal (a deep-copied flagship
    # scenario failed its own clean capture), and `model_dump_json()` could not
    # serialise a bare object at all. A fields-set is copied with the model and
    # serialises like any other state, and `from: null` stays a real endpoint
    # because what decides is that the key was written, not what it holds. A
    # dump that must PRESERVE the distinction needs `exclude_unset=True`: a
    # plain dump writes both keys, so re-validating one turns an omitted
    # endpoint into a pin on null, which is a narrower claim than was made.
    from_: Any = Field(default=None, alias="from")
    to: Any = None

    @model_validator(mode="after")
    def _check(self) -> "TransitionSpec":
        if len(self.key) != 1:
            raise ValueError("transition key must be exactly one column: value pair")
        require_cell_values("key", self.key)
        # `from`/`to` are compared against the changed cell's own values, so
        # they live in the same domain the key does. Presence decides, not
        # truthiness: an omitted endpoint matches anything, while `from: null`
        # is a real endpoint (a timestamp column starts out null).
        if self.has_from:
            require_cell_value("from", self.from_)
        if self.has_to:
            require_cell_value("to", self.to)
        return self

    @property
    def has_from(self) -> bool:
        return "from_" in self.model_fields_set

    @property
    def has_to(self) -> bool:
        return "to" in self.model_fields_set


class CountDeltaSpec(_EffectBase):
    table: str
    added: CountSpec | None = None
    removed: CountSpec | None = None
    match: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check(self) -> "CountDeltaSpec":
        if self.added is None and self.removed is None:
            raise ValueError("count_delta needs at least one of added/removed")
        require_cell_values("match", self.match)
        return self


class UnchangedSpec(_EffectBase):
    table: str
    where: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check(self) -> "UnchangedSpec":
        require_cell_values("where", self.where)
        return self


class EventExistsSpec(_EffectBase):
    type: str
    count: CountSpec
    entity_type: str | None = None
    entity_id: str | None = None
    actor: str | None = None
    system: str | None = None
    payload_includes: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check(self) -> "EventExistsSpec":
        # Payload pins are matched against JSON, not against table cells, so
        # they get the wider domain. The identity filters above are typed as
        # strings by the model itself, which leaves exactly one thing to refuse
        # in them: text an artifact cannot carry at all.
        require_payload_values("payload_includes", self.payload_includes)
        require_event_filters(self)
        return self


class IdempotentSpec(_EffectBase):
    mode: Literal["unique_effect"]
    table: str
    key_fields: list[str] = Field(min_length=1)
    scope: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check(self) -> "IdempotentSpec":
        require_cell_values("scope", self.scope)
        return self


class EventSelector(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    payload_key: str

    @model_validator(mode="after")
    def _check(self) -> "EventSelector":
        # `payload_key` is a JSON object key looked up in an event payload, so a
        # lone surrogate reaches no key an artifact can carry. `type` is left to
        # `require_event_type`, which each evaluator calls. See
        # `require_event_selector` for the runtime half of this refusal.
        require_encodable_text("payload_key", self.payload_key)
        return self


class NetState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table: str
    where: dict[str, Any]
    expect_count: ExactCount

    @model_validator(mode="after")
    def _check(self) -> "NetState":
        require_cell_values("net_state.where", self.where)
        return self


class CompensatedSpec(_EffectBase):
    open_event: EventSelector
    close_event: EventSelector
    net_state: NetState

    @model_validator(mode="after")
    def _check(self) -> "CompensatedSpec":
        if self.open_event.type == self.close_event.type:
            raise ValueError("compensated open_event and close_event types must differ")
        return self


class CorrelatedSpec(_EffectBase):
    # `count` defaults to exactly one: the reason this rule exists is that a
    # row and its audit event were only ever required to exist separately, and
    # one-audit-event-per-row is what "separately" was missing. An author who
    # means something looser has to say so.
    table: str
    event: EventSelector
    match: dict[str, Any] | None = None
    count: CountSpec = 1

    @model_validator(mode="after")
    def _check(self) -> "CorrelatedSpec":
        require_cell_values("match", self.match)
        return self


RULE_MODELS: dict[str, type[_EffectBase]] = {
    "transition": TransitionSpec,
    "count_delta": CountDeltaSpec,
    "unchanged": UnchangedSpec,
    "event_exists": EventExistsSpec,
    "idempotent": IdempotentSpec,
    "compensated": CompensatedSpec,
    "correlated": CorrelatedSpec,
}
INVARIANT_RULES = frozenset({"unchanged", "idempotent", "compensated", "correlated"})
RESERVED_EFFECT_IDS = frozenset({"append_only", "unexplained", "artifact", "verdict"})
ListName = Literal["expected", "allowed", "forbidden"]


class Effect(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    rule: str
    list_name: ListName
    spec: Any


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirements: list[str] = Field(default_factory=list)
    turns: list[str] = Field(default_factory=list)
    silobench_task: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "Provenance":
        # Provenance is copied verbatim into the verdict's `provenance` object
        # and serialized with it, so a lone surrogate here has no UTF-8 encoding
        # and `check --json` dies writing the verdict rather than producing the
        # machine-readable error every failure mode is promised.
        for index, requirement in enumerate(self.requirements):
            require_encodable_text(f"provenance.requirements[{index}]", requirement)
        for index, turn in enumerate(self.turns):
            require_encodable_text(f"provenance.turns[{index}]", turn)
        require_encodable_text("provenance.silobench_task", self.silobench_task)
        return self


class RulesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_change: Literal["forbid", "allow"] = "forbid"


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: Literal["statediff.scenario.v1"]
    id: str
    title: str
    adapter: Literal["silobench"]
    provenance: Provenance = Field(default_factory=Provenance)
    # A scenario that declares nothing cannot pass. With an empty list the only
    # checks left are the append-only prefix and the unexplained sweep, and an
    # unchanged pair satisfies both, so the verdict reads `pass` over a run
    # nothing was ever asserted about. `_parse_effects` refuses this for a
    # parsed file and the model refuses it for one built in Python, which is
    # the same guarantee the rest of this module makes for selector values.
    effects: list[Effect] = Field(min_length=1)
    rules_config: RulesConfig = Field(default_factory=RulesConfig)

    @model_validator(mode="after")
    def _check(self) -> "Scenario":
        # `id` and `title` are written verbatim into the human report and into
        # the verdict JSON as `scenario_id`/`title`. Pydantic refuses a lone
        # surrogate in a length-constrained string (an effect id) but not in a
        # plain one, so these two slipped through and the report and `check
        # --json` then died with an encoding error instead of the machine-
        # readable error verdict a bad scenario is promised.
        require_encodable_text("id", self.id)
        require_encodable_text("title", self.title)
        return self

    def by_list(self, list_name: ListName) -> list[Effect]:
        return [effect for effect in self.effects if effect.list_name == list_name]


def _parse_effects(raw_effects: Any) -> list[Effect]:
    if not isinstance(raw_effects, dict):
        raise ScenarioError("effects must be a mapping of expected/allowed/forbidden lists")
    unknown_lists = set(raw_effects) - {"expected", "allowed", "forbidden"}
    if unknown_lists:
        # Sorted by type name first, the way the diff orders atom keys: YAML
        # mapping keys are whatever the document wrote, so a file naming both
        # `1` and `bogus` made a plain `sorted` raise TypeError from outside
        # every handler that turns a bad scenario into an `error` verdict, and
        # `check --json` died with a traceback instead of the machine-readable
        # refusal it promises. The names are still reported as themselves.
        listed = sorted(unknown_lists, key=lambda name: (type(name).__name__, str(name)))
        raise ScenarioError(f"unknown effect lists: {listed}")
    effects: list[Effect] = []
    ids: set[str] = set()
    for list_name in ("expected", "allowed", "forbidden"):
        entries = raw_effects.get(list_name)
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise ScenarioError(f"effects.{list_name} must be a list, got {type(entries).__name__}")
        for item in entries:
            if not isinstance(item, dict) or len(item) != 1:
                raise ScenarioError(f"each effect must be a single rule mapping, got: {item!r}")
            rule, params = next(iter(item.items()))
            model = RULE_MODELS.get(rule)
            if model is None:
                raise ScenarioError(f"unknown rule '{rule}' (known: {sorted(RULE_MODELS)})")
            if rule in INVARIANT_RULES and list_name != "expected":
                raise ScenarioError(f"invariant rule '{rule}' is only valid under expected, not {list_name}")
            try:
                spec = model.model_validate(params)
            except ValidationError as exc:
                raise ScenarioError(f"invalid {rule} effect: {exc}") from exc
            if spec.id in ids:
                raise ScenarioError(f"duplicate effect id '{spec.id}'")
            if spec.id in RESERVED_EFFECT_IDS:
                raise ScenarioError(f"effect id '{spec.id}' is reserved for verdict-level checks")
            ids.add(spec.id)
            effects.append(Effect(rule=rule, list_name=list_name, spec=spec))
    validate_effects(effects)
    return effects


def validate_effects(effects: list[Effect]) -> None:
    """Scenario-level invariants enforced on every scenario the engine evaluates.

    ``_parse_effects`` checks these while reading a document, but a library caller
    can build ``Scenario``/``Effect`` objects directly (or via ``model_construct``
    and ``model_copy(update=...)``, which skip validation), reaching the engine on
    a path that never saw them. The engine calls this before evaluating, so the
    same scenario cannot mean one thing loaded from a file and another built in
    Python. Kept beside ``_parse_effects`` so the two agree by construction.
    """
    if not effects:
        raise ScenarioError("scenario declares no effects, so nothing about this run was asserted")
    if not any(e.list_name in ("expected", "forbidden") for e in effects):
        # Allowed effects justify coverage; they never fail. A scenario with only
        # allowed effects therefore asserts nothing that can fail, and against an
        # unchanged before/after it clears while proving no task was done. A
        # meaningful scenario requires at least one expected effect (a change that
        # must happen) or one forbidden effect (a change that must not).
        raise ScenarioError(
            "scenario declares only allowed effects, which justify coverage but assert nothing "
            "that can fail; a pass would not mean the task was done"
        )
    ids: set[str] = set()
    for effect in effects:
        if effect.rule in INVARIANT_RULES and effect.list_name != "expected":
            raise ScenarioError(
                f"invariant rule '{effect.rule}' is only valid under expected, not "
                f"{effect.list_name}"
            )
        if effect.spec.id in ids:
            raise ScenarioError(f"duplicate effect id '{effect.spec.id}'")
        if effect.spec.id in RESERVED_EFFECT_IDS:
            raise ScenarioError(f"effect id '{effect.spec.id}' is reserved for verdict-level checks")
        ids.add(effect.spec.id)


_TIMESTAMP_TAG = "tag:yaml.org,2002:timestamp"


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader with two scenario-specific rules.

    Duplicate mapping keys are rejected: a repeated `forbidden` list or rule
    parameter silently replacing an earlier one could drop policy checks.

    Unquoted ISO dates and timestamps stay strings. Snapshot cells are only
    ever strings, integers, or null, so a selector value decoded into a
    `datetime.date` matches no row at all: `where: {received_date: 2026-01-03}`
    would select nothing and report the policy check as a pass over an empty
    set, which is exactly the silent non-match the rest of this module refuses.
    """


# The resolver table is COPIED before the timestamp entry is dropped: it lives
# on yaml.resolver.Resolver and is shared by every loader class in the process,
# so removing the entry in place would change date handling for every other
# yaml user too.
_StrictLoader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag != _TIMESTAMP_TAG]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _no_duplicates(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if isinstance(key, (str, int, bool, type(None))) and key in seen:
            raise ScenarioError(f"duplicate mapping key {key!r} in scenario")
        seen.add(key if isinstance(key, (str, int, bool, type(None))) else id(key_node))
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates
)


def load_scenario(path: str | Path) -> Scenario:
    path = Path(path)
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictLoader)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ScenarioError(f"cannot read scenario {path}: {exc}") from exc
    except RecursionError as exc:
        # A scenario nested thousands of collections deep exhausts the YAML
        # parser's stack. RecursionError is not a YAMLError, so without this it
        # escaped `load_scenario` as a traceback rather than the ScenarioError
        # that every other unreadable scenario becomes.
        raise ScenarioError(f"scenario {path} is nested too deeply to parse") from exc
    return parse_scenario(raw)


def parse_scenario(raw: Any) -> Scenario:
    if not isinstance(raw, dict):
        raise ScenarioError("scenario document must be a mapping")
    raw = dict(raw)
    effects = _parse_effects(raw.pop("effects", None))
    try:
        return Scenario.model_validate({**raw, "effects": effects})
    except ValidationError as exc:
        raise ScenarioError(f"invalid scenario: {exc}") from exc
