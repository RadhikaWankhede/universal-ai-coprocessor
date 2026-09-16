"""Tool schemas must come from the same manifests the broker enforces."""
from __future__ import annotations

from uaic.broker import default_registry, tool_schema, tool_schemas


def test_every_declared_tool_gets_a_schema():
    reg = default_registry()
    assert len(tool_schemas(reg)) == len(reg)


def test_the_model_can_never_see_a_tool_the_broker_does_not_know():
    """One source of truth. Drift here would look like a model failure."""
    reg = default_registry()
    seen = {s["name"] for s in tool_schemas(reg)}
    assert seen == set(reg.names())


def test_required_matches_the_manifest():
    reg = default_registry()
    for schema in tool_schemas(reg):
        spec = reg.lookup(schema["name"])
        assert set(schema["input_schema"]["required"]) == set(spec.required_args())


def test_optional_arguments_are_not_required():
    schema = tool_schema(default_registry().lookup("fs.delete"))
    assert "recursive" in schema["input_schema"]["properties"]
    assert "recursive" not in schema["input_schema"]["required"]
    assert "path" in schema["input_schema"]["required"]


def test_paths_are_strings_with_a_hint():
    prop = tool_schema(default_registry().lookup("fs.move"))["input_schema"]["properties"]
    assert prop["src"]["type"] == "string"
    assert "absolute" in prop["src"]["description"]


def test_booleans_map_to_boolean():
    prop = tool_schema(default_registry().lookup("fs.delete"))["input_schema"]["properties"]
    assert prop["recursive"]["type"] == "boolean"


def test_no_extra_arguments_are_accepted():
    for schema in tool_schemas(default_registry()):
        assert schema["input_schema"]["additionalProperties"] is False


def test_descriptions_are_never_empty():
    for schema in tool_schemas(default_registry()):
        assert schema["description"].strip()


def test_schema_order_is_stable():
    reg = default_registry()
    assert [s["name"] for s in tool_schemas(reg)] == list(reg.names())
