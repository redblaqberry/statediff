"""Canonical JSON and sha256, byte-compatible with SiloBench's hash module.

The contract (silobench docs/formats.md): object keys sorted by UTF-16 code
units, arrays in order, no whitespace, UTF-8 encoded before sha256. Sorting by
the UTF-16BE encoding of the key gives exactly UTF-16 code-unit order, which
differs from Python's default code-point ordering for surrogate-pair
characters; the committed cross-language vectors pin this.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


class CanonicalError(ValueError):
    """A value that cannot appear in a v1 artifact reached the canonicalizer."""


def _utf16_key(key: str) -> bytes:
    try:
        return key.encode("utf-16-be")
    except UnicodeEncodeError as exc:
        raise CanonicalError(f"key contains a lone surrogate and cannot be ordered: {key!r}") from exc


def strict_equal(a: Any, b: Any) -> bool:
    """JSON-value equality that never confuses booleans, integers, and floats.

    Python's `1 == True` and `1 == 1.0` both hold, but canonical JSON writes
    all three differently, so any comparison feeding a verdict must be
    type-strict. The int/float case matters most where two independently
    produced artifacts are reconciled: a snapshot payload holding `210000` and
    a JSONL export holding `210000.0` are not the same export, and treating
    them as equal would let a bundle claim a cross-check the two artifacts do
    not actually survive.
    """
    # bool first: `isinstance(True, int)` is true, so the boolean case has to be
    # settled before the numeric one or every boolean reads as the integer it
    # is not.
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, int) != isinstance(b, int):
        return False
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(strict_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(strict_equal(x, y) for x, y in zip(a, b))
    return a == b


def _write(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        out.append(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, int):
        out.append(str(value))
    elif isinstance(value, float):
        raise CanonicalError("floats do not occur in v1 artifacts; refusing to guess a formatting")
    elif isinstance(value, list):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _write(item, out)
        out.append("]")
    elif isinstance(value, dict):
        out.append("{")
        # Checked before sorting: the sort key calls .encode on every key, so a
        # non-string key would surface as an AttributeError from inside sorted()
        # rather than as the refusal this function promises.
        for key in value.keys():
            if not isinstance(key, str):
                raise CanonicalError(f"non-string object key: {key!r}")
        for index, key in enumerate(sorted(value.keys(), key=_utf16_key)):
            if index:
                out.append(",")
            out.append(json.dumps(key, ensure_ascii=False))
            out.append(":")
            _write(value[key], out)
        out.append("}")
    else:
        raise CanonicalError(f"unsupported type in artifact: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    parts: list[str] = []
    _write(value, parts)
    return "".join(parts)


def sha256_hex(text: str) -> str:
    try:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    except UnicodeEncodeError as exc:
        raise CanonicalError(f"value contains a lone surrogate and cannot be hashed: {exc}") from exc
