"""Minimal JSON-Schema validator (the subset HYDRA's structured calls use).

Strict conformance checking for ``format`` replies (T07/F1): type, enum,
required keys, properties, additionalProperties=false, items.  Returns a list
of human-readable error strings; an empty list means the value is valid.
No third-party imports; no schema features beyond the ones listed above.
"""
from __future__ import annotations

_TYPE_MAP = {"object": dict, "array": list, "string": str,
             "boolean": bool, "null": type(None)}


def type_name(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _matches(value, expected: str) -> bool:
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    py = _TYPE_MAP.get(expected)
    return py is not None and isinstance(value, py)


def validate(value, schema, path: str = "$") -> list[str]:
    """Validate ``value`` against ``schema``; return a list of error strings."""
    errors: list[str] = []
    if not isinstance(schema, dict):
        return errors
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_matches(value, t) for t in types):
            return [f"{path}: expected {'/'.join(types)}, got {type_name(value)}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in {schema['enum']!r}")
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                errors.append(f"{path}: missing required key {key!r}")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    errors.append(f"{path}: unexpected key {key!r}")
        for key, sub in props.items():
            if key in value:
                errors.extend(validate(value[key], sub, f"{path}.{key}"))
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            errors.extend(validate(item, schema["items"], f"{path}[{i}]"))
    return errors
