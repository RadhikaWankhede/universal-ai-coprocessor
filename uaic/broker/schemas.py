"""Tool schemas for the model, generated from the SAME manifests the broker
validates against.

Two independent sources would drift, and every drift would look like a model
failure while actually being a build defect.
"""
from __future__ import annotations

from typing import Any

from uaic.broker.manifest import Registry, ToolSpec

JSON_TYPE = {
    "path": "string",
    "str": "string",
    "email": "string",
    "bool": "boolean",
    "int": "integer",
}

HINT = {
    "path": "An absolute filesystem path.",
    "email": "An email address.",
}


def tool_schema(spec: ToolSpec) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for name, arg in spec.args.items():
        prop: dict[str, Any] = {"type": JSON_TYPE[arg.type]}
        if arg.type in HINT:
            prop["description"] = HINT[arg.type]
        if arg.default is not None:
            prop["default"] = arg.default
        properties[name] = prop

    return {
        "name": spec.name,
        "description": spec.summary or spec.name,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": sorted(spec.required_args()),
            "additionalProperties": False,
        },
    }


def tool_schemas(registry: Registry) -> list[dict[str, Any]]:
    """Every declared tool, in a stable order."""
    return [tool_schema(registry.lookup(n)) for n in registry.names()]
