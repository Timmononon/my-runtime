import copy
import json

import pytest

from aster.evaluation.bfcl import BFCLUnsupportedError, grade_case, load_cases, public_case, tool_specs


def files(tmp_path, *, category="simple_python", functions=None, truth=None, questions=None):
    functions = functions or [{"name": "math.factorial", "description": "Calculate the factorial.", "parameters": {
        "type": "dict", "properties": {"number": {"type": "integer"}}, "required": ["number"]}}]
    row = {"id": category + "_0", "question": questions or [[{"role": "user", "content": "Calculate factorial of 5."}]],
           "function": functions}
    truth = [{"math.factorial": {"number": [5]}}] if truth is None else truth
    data, answers = tmp_path / "BFCL_v4_simple_python.json", tmp_path / "answers.jsonl"
    data.write_text(json.dumps(row) + "\n", encoding="utf-8")
    answers.write_text(json.dumps({"id": row["id"], "ground_truth": truth}) + "\n", encoding="utf-8")
    return data, answers


def case(tmp_path, **kwargs):
    return load_cases(*files(tmp_path, **kwargs))[0]


def call(name="math.factorial", number=5):
    return {"name": name, "arguments": {"number": number}}


def test_official_shape_alias_and_input_digest(tmp_path):
    value = case(tmp_path)
    spec = tool_specs(value)[0]
    assert spec.name.startswith("b_") and "." not in spec.name
    assert value["tool_names"][spec.name] == "math.factorial"
    assert spec.parameters["type"] == "object"
    assert value["provenance"]["source_version"] == "v4"
    assert len(value["provenance"]["data_sha256"]) == 64
    assert grade_case(value, [call(spec.name)])["passed"]


@pytest.mark.parametrize("calls", [[call(number=6)], [call(number=True)], [call(number="5")],
                                  [call("unknown")], [call(), call()], []])
def test_wrong_arguments_unknown_and_extra_calls_cannot_pass(tmp_path, calls):
    result = grade_case(case(tmp_path), calls)
    assert result["result"] == "fail" and not result["passed"]
    assert result["official_score"] is False


def test_answers_and_arbitrary_source_fields_cannot_enter_public_projection(tmp_path):
    value = case(tmp_path)
    value["unexpected"] = {"secret": "PRIVATE_ANSWER"}
    value["expected"][0]["math.factorial"]["number"] = [9248367]
    public = public_case(value)
    assert set(public) == {"id", "category", "prompt", "messages"}
    assert "9248367" not in json.dumps(public)
    assert "PRIVATE_ANSWER" not in json.dumps(public)
    public["messages"][0]["content"] = "changed"
    assert value["messages"][0]["content"] != "changed"
    assert "9248367" not in json.dumps([vars(spec) for spec in tool_specs(value)])


def test_optional_sentinel_and_alternative_values(tmp_path):
    fn = {"name": "weather.get", "description": "Get weather.", "parameters": {"type": "dict", "properties": {
        "city": {"type": "string"}, "unit": {"type": "string"}}, "required": ["city"]}}
    value = case(tmp_path, functions=[fn], truth=[{"weather.get": {"city": ["Paris", "Lyon"], "unit": ["", "C"]}}])
    for args in ({"city": "Paris"}, {"city": "Lyon", "unit": "C"}):
        assert grade_case(value, [{"name": "weather.get", "arguments": args}])["passed"]
    for args in ({"city": "Paris", "unit": "F"}, {"city": "paris"}, {"city": "Paris", "other": 1}):
        assert not grade_case(value, [{"name": "weather.get", "arguments": args}])["passed"]


def test_required_argument_not_omittable_even_with_empty_sentinel(tmp_path):
    value = case(tmp_path, truth=[{"math.factorial": {"number": ["", 5]}}])
    assert not grade_case(value, [{"name": "math.factorial", "arguments": {}}])["passed"]


def test_parallel_matching_is_order_independent_and_preserves_duplicates(tmp_path):
    value = case(tmp_path, category="parallel", truth=[{"math.factorial": {"number": [5]}},
                                                      {"math.factorial": {"number": [6]}}])
    assert grade_case(value, [call(number=6), call(number=5)])["passed"]
    assert not grade_case(value, [call(number=5), call(number=5)])["passed"]


def test_parallel_overlapping_candidates_use_complete_assignment(tmp_path):
    value = case(tmp_path, category="parallel", truth=[{"math.factorial": {"number": [5, 6]}},
                                                      {"math.factorial": {"number": [5]}}])
    assert grade_case(value, [call(number=5), call(number=6)])["passed"]


def test_type_named_property_and_bfcl_python_annotations(tmp_path):
    fn = {"name": "measure", "description": "Measure.", "parameters": {"type": "dict", "properties": {
        "type": {"type": "string"}, "amount": {"type": "float"},
        "coordinates": {"type": "tuple", "items": {"type": "float"}}, "metadata": {"type": "any"}},
        "required": ["type", "amount", "coordinates", "metadata"]}}
    truth = [{"measure": {"type": ["distance"], "amount": [2.5], "coordinates": [[1, 2]], "metadata": [{"x": True}]}}]
    value = case(tmp_path, functions=[fn], truth=truth)
    args = {"type": "distance", "amount": 2.5, "coordinates": [1, 2], "metadata": {"x": True}}
    assert grade_case(value, [{"name": "measure", "arguments": args}])["passed"]
    args["metadata"]["x"] = 1
    assert not grade_case(value, [{"name": "measure", "arguments": args}])["passed"]


def test_irrelevance_requires_explicit_empty_ground_truth_and_no_calls(tmp_path):
    value = case(tmp_path, category="irrelevance", truth=[])
    assert grade_case(value, [], answer="This tool cannot do that.")["passed"]
    assert not grade_case(value, [call()])["passed"]


def test_nested_dict_and_array_of_dict_accepted_values(tmp_path):
    condition = {"type": "dict", "properties": {"field": {"type": "string"}, "values": {
        "type": "array", "items": {"type": "integer"}}}, "required": ["field", "values"]}
    fn = {"name": "database.query", "description": "Query.", "parameters": {"type": "dict", "properties": {
        "conditions": {"type": "array", "items": condition}}, "required": ["conditions"]}}
    truth = [{"database.query": {"conditions": [[{"field": ["age", "years"], "values": [[1, 2]]}]]}}]
    value = case(tmp_path, functions=[fn], truth=truth)
    args = {"conditions": [{"field": "years", "values": [1, 2]}]}
    assert grade_case(value, [{"name": "database.query", "arguments": args}])["passed"]
    args["conditions"][0]["values"] = [2, 1]
    assert not grade_case(value, [{"name": "database.query", "arguments": args}])["passed"]


@pytest.mark.parametrize("category", ["multi_turn_base", "multi_turn_miss_func", "simple_java", "simple_javascript", "memory"])
def test_unsupported_tracks_rejected_before_any_execution(tmp_path, category):
    with pytest.raises(BFCLUnsupportedError):
        load_cases(*files(tmp_path, category=category))


def test_multiple_user_turns_not_mislabeled_single_turn(tmp_path):
    with pytest.raises(BFCLUnsupportedError):
        case(tmp_path, questions=[[{"role": "user", "content": "First."}], [{"role": "user", "content": "Second."}]])


def test_dataset_and_answer_pair_must_match_exactly(tmp_path):
    data, answers = files(tmp_path)
    answers.write_text('{"id":"simple_python_1","ground_truth":[]}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="IDs must match"):
        load_cases(data, answers)


@pytest.mark.parametrize("payload", [
    '{"id":"simple_python_0","id":"simple_python_1"}\n',
    '{"id":"simple_python_0","ground_truth":NaN}\n',
    '{"id":"simple_python_0","ground_truth":1e999}\n',
    '{"id":"simple_python_0","ground_truth":[]}\n{"id":"simple_python_0","ground_truth":[]}\n',
])
def test_ambiguous_nonfinite_and_duplicate_json_rejected(tmp_path, payload):
    data, answers = files(tmp_path)
    answers.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError):
        load_cases(data, answers)


def test_missing_ground_truth_and_undeclared_reference_rejected(tmp_path):
    data, answers = files(tmp_path)
    answers.write_text('{"id":"simple_python_0"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="no ground_truth"):
        load_cases(data, answers)
    files(tmp_path, truth=[{"other": {"number": [5]}}])
    with pytest.raises(ValueError, match="undeclared function"):
        load_cases(data, answers)


def test_alias_collisions_fail_closed(tmp_path):
    value = case(tmp_path)
    alias = tool_specs(value)[0].name
    functions = [copy.deepcopy(value["functions"][0]), {"name": alias, "description": "Collision.",
                 "parameters": {"type": "dict", "properties": {}, "required": []}}]
    with pytest.raises(ValueError, match="alias"):
        case(tmp_path, functions=functions)


def test_does_not_parse_or_execute_model_text(tmp_path):
    value = case(tmp_path)
    assert not grade_case(value, [], answer="math.factorial(number=5)")["passed"]
    assert not grade_case(value, ["__import__('os').system('not executed')"])["passed"]
    assert not grade_case(value, [{"name": "math.factorial", "arguments": {"number": float("nan")}}])["passed"]


def test_json_schema_cannot_trigger_reference_network_fetch(tmp_path):
    fn = {"name": "remote", "description": "No network.", "parameters": {"type": "dict", "properties": {
        "number": {"$ref": "https://not-fetched.invalid/schema"}}, "required": ["number"]}}
    with pytest.raises(BFCLUnsupportedError, match="References"):
        case(tmp_path, functions=[fn], truth=[{"remote": {"number": [5]}}])
