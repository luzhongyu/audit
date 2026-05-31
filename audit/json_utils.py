"""Robust JSON extraction + schema validation for agent outputs."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

from jsonschema import Draft7Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)

# The model sometimes writes output to a file and only mentions it in the
# text response, e.g. "Output written to `recon_output.json`".
_WROTE_FILE_RE = re.compile(
    r"(?:output|wrote|written|saved)\s+(?:to|in)\s+[`'\"]?(\S+\.json)[`'\"]?",
    re.IGNORECASE,
)


def extract_json(text: str, *, cwd: Path | None = None) -> Any:
    """Pull a JSON object out of an assistant message.

    Order of attempts:
      1. The full text is valid JSON.
      2. The model wrote JSON to a file — read it from *cwd*.
      3. The text contains a ```json ... ``` fenced block.
      4. The largest balanced {...} or [...] substring is valid JSON.

    Raises ValueError if no JSON can be extracted.
    """
    text = text.strip()
    if not text:
        raise ValueError("Empty assistant output.")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    if cwd is not None:
        m = _WROTE_FILE_RE.search(text)
        if m:
            file_path = cwd / m.group(1)
            if file_path.exists():
                try:
                    data = json.loads(file_path.read_text())
                    log.info("extract_json: read %s (%d bytes)", file_path, file_path.stat().st_size)
                    return data
                except (json.JSONDecodeError, OSError) as e:
                    log.debug("extract_json: failed to read %s: %s", file_path, e)

    m = _FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    candidate = _largest_balanced(text)
    if candidate is not None:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    raise ValueError(
        f"Could not extract JSON from assistant output (len={len(text)}). "
        f"Head: {text[:200]!r}"
    )


def _largest_balanced(text: str) -> str | None:
    """Return the largest balanced {...} or [...] substring, or None."""
    best: str | None = None
    for open_c, close_c in (("{", "}"), ("[", "]")):
        for i, ch in enumerate(text):
            if ch != open_c:
                continue
            depth = 0
            in_str = False
            esc = False
            for j in range(i, len(text)):
                c = text[j]
                if esc:
                    esc = False
                    continue
                if c == "\\":
                    esc = True
                    continue
                if c == '"' and not esc:
                    in_str = not in_str
                if in_str:
                    continue
                if c == open_c:
                    depth += 1
                elif c == close_c:
                    depth -= 1
                    if depth == 0:
                        candidate = text[i : j + 1]
                        if best is None or len(candidate) > len(best):
                            best = candidate
                        break
    return best


def validate_schema(payload: Any, schema_path: Path) -> list[str]:
    """Validate `payload` against the schema at `schema_path`.

    Sibling schemas in the same directory are loaded into a referencing
    Registry so `$ref` entries like `"hunt_task.schema.json"` resolve.

    Returns a list of human-readable error strings; empty means valid.
    """
    schema = json.loads(schema_path.read_text())
    schemas_dir = schema_path.parent.resolve()

    registry: Registry = Registry()
    for sf in schemas_dir.glob("*.schema.json"):
        raw = json.loads(sf.read_text())
        registry = registry.with_resource(
            sf.name, Resource.from_contents(raw, default_specification=DRAFT7)
        )

    validator = Draft7Validator(schema, registry=registry)
    return [
        f"{'/'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message}"
        for err in sorted(validator.iter_errors(payload), key=lambda e: e.path)
    ]
