"""Regression tests for the two production LLM-integration bugs:
1. Gemini 400'd on EVERY tool call because _to_schema flattened away `items` /
   nested properties.
2. Groq 400'd (tool_use_failed) when gpt-oss passed `null` for optional args.
"""
from DivineService.llm_gemini import _to_schema
from DivineService.llm_groq import _recover_tool_call_from_error, GroqError
from DivineService.service_chatbot import TOOL_SCHEMAS, _make_optionals_nullable


# ---- Gemini _to_schema recursion ----

def test_to_schema_keeps_array_items():
    s = _to_schema({"type": "object", "properties": {
        "opts": {"type": "array", "items": {"type": "number"}},
    }, "required": []})
    arr = s.properties["opts"]
    assert str(arr.type).upper().endswith("ARRAY")
    assert arr.items is not None and str(arr.items.type).upper().endswith("NUMBER")


def test_to_schema_defaults_missing_array_items_instead_of_emitting_invalid_schema():
    s = _to_schema({"type": "object", "properties": {"opts": {"type": "array"}}})
    assert s.properties["opts"].items is not None  # Gemini would 400 without this


def test_to_schema_maps_nullable_union_type():
    s = _to_schema({"type": "object", "properties": {
        "rate": {"type": ["number", "null"]},
    }})
    p = s.properties["rate"]
    assert str(p.type).upper().endswith("NUMBER")
    assert p.nullable is True


def test_to_schema_recurses_into_nested_object():
    s = _to_schema({"type": "object", "properties": {
        "person": {"type": "object", "properties": {"age": {"type": "number"}}},
    }})
    assert s.properties["person"].properties["age"] is not None


def test_every_shipped_tool_schema_converts_without_error():
    for tool in TOOL_SCHEMAS:
        _to_schema(tool["parameters"])  # must not raise


def test_no_array_property_reaches_gemini_without_items():
    for tool in TOOL_SCHEMAS:
        schema = _to_schema(tool["parameters"])
        for name, prop in (schema.properties or {}).items():
            if str(getattr(prop, "type", "")).upper().endswith("ARRAY"):
                assert prop.items is not None, f"{tool['name']}.{name} array has no items"


# ---- optional-params-nullable pass ----

def test_make_optionals_nullable_marks_only_non_required():
    patched = _make_optionals_nullable([{
        "name": "t", "description": "d",
        "parameters": {"type": "object",
                       "properties": {"a": {"type": "string"}, "b": {"type": "number"}},
                       "required": ["a"]},
    }])
    props = patched[0]["parameters"]["properties"]
    assert props["a"]["type"] == "string"            # required -> untouched
    assert props["b"]["type"] == ["number", "null"]  # optional -> nullable


def test_shipped_loan_tools_have_nullable_optionals():
    by_name = {t["name"]: t for t in TOOL_SCHEMAS}
    emi = by_name["calculate_emi"]["parameters"]["properties"]
    assert emi["annual_rate_pct"]["type"] == ["number", "null"]
    ulp = by_name["update_loan_profile"]["parameters"]["properties"]
    assert ulp["property_price"]["type"] == ["string", "null"]


# ---- Groq tool_use_failed salvage ----

class _FakeGroqBadRequest(Exception):
    def __init__(self, body):
        super().__init__("400")
        self.body = body


def test_recover_tool_call_from_tool_use_failed():
    exc = _FakeGroqBadRequest({"error": {
        "code": "tool_use_failed",
        "failed_generation": '{"name": "calculate_emi", "arguments": {"principal": 4000000, "annual_rate_pct": null, "tenure_years": 20}}',
    }})
    got = _recover_tool_call_from_error(exc)
    assert got == {"name": "calculate_emi", "args": {"principal": 4000000, "tenure_years": 20}}


def test_recover_handles_stringified_arguments_and_drops_all_nulls():
    exc = _FakeGroqBadRequest({"error": {
        "code": "tool_use_failed",
        "failed_generation": '{"name": "update_loan_profile", "arguments": {"monthly_income": null, "requested_loan": null}}',
    }})
    assert _recover_tool_call_from_error(exc) == {"name": "update_loan_profile", "args": {}}


def test_recover_returns_none_for_unrelated_errors():
    assert _recover_tool_call_from_error(RuntimeError("timeout")) is None
    assert _recover_tool_call_from_error(_FakeGroqBadRequest({"error": {"code": "rate_limit_exceeded"}})) is None
    assert _recover_tool_call_from_error(_FakeGroqBadRequest("not a dict")) is None
