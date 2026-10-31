import copy
import json
import os
from decimal import getcontext

import pytest

from aster.evaluation.bfcl_v4_tools import BFCLExecutionUnsupported, create_environment, parse_call


def filesystem_case():
    definitions = {
        "touch": ({"file_name": {"type": "string"}}, ["file_name"]),
        "echo": ({"content": {"type": "string"}, "file_name": {"type": "string"}}, ["content"]),
        "cat": ({"file_name": {"type": "string"}}, ["file_name"]),
        "pwd": ({}, []),
    }
    functions = [{"name": name, "description": "Offline simulated file operation.",
                  "parameters": {"type": "dict", "properties": properties, "required": required}}
                 for name, (properties, required) in definitions.items()]
    return {"id": "multi_turn_base_fixture", "category": "multi_turn_base", "variant": None,
            "functions": functions, "involved_classes": ["GorillaFileSystem"],
            "initial_config": {"GorillaFileSystem": {"root": {
                "sandbox": {"type": "directory", "contents": {}}}}},
            "question": [[{"role": "user", "content": "Create memo.txt containing alpha."}],
                         [{"role": "user", "content": "Read the file we just created."}]],
            "expected": [["touch(file_name='memo.txt')", "echo(content='alpha', file_name='memo.txt')"],
                         ["cat(file_name='memo.txt')"]], "missed_function": {}, "excluded_function": []}


def test_literal_call_parser_and_forbidden_python_never_run(tmp_path):
    assert parse_call("mean(numbers=[1, -2, 3.5])") == (None, "mean", [], {"numbers": [1, -2, 3.5]})
    assert parse_call("MathAPI.mean([1,2])") == ("MathAPI", "mean", [[1, 2]], {})
    marker = tmp_path / "must-not-exist"
    invalid = [f"__import__('pathlib').Path({str(marker)!r}).touch()", "open('x')", "x.__class__()",
               "MathAPI.__dict__.get('x')", "mean(numbers=[x for x in [1]])", "mean(numbers=(lambda: 1)())",
               "mean(**{'numbers':[1]})", "mean(numbers=[1e999])", "mean(numbers=set())", "touch(file_name='a');exit()"]
    for expression in invalid:
        with pytest.raises(BFCLExecutionUnsupported):
            parse_call(expression)
    assert not marker.exists()


def test_case_state_isolation_and_no_host_file_process_or_environment_changes(tmp_path, monkeypatch):
    def prohibited(*args, **kwargs):
        raise AssertionError("BFCL local simulated filesystem must not start host processes")

    monkeypatch.setattr("subprocess.run", prohibited)
    monkeypatch.setattr("subprocess.Popen", prohibited)
    initial = filesystem_case()
    before = copy.deepcopy(initial)
    cwd, environment = os.getcwd(), dict(os.environ)
    model = create_environment(initial, root=tmp_path / "model")
    reference = create_environment(initial, root=tmp_path / "reference")
    model.call_expressions(initial["expected"][0])
    assert json.loads(model.execute("cat", {"file_name": "memo.txt"})) == {"file_content": "alpha"}
    assert "error" in json.loads(reference.call_expressions(["cat(file_name='memo.txt')"])[0])
    assert model.decode_call("cat('memo.txt')") == {"name": "cat", "arguments": {"file_name": "memo.txt"}}
    assert not (tmp_path / "model").exists()
    assert not (tmp_path / "memo.txt").exists()
    assert initial == before and cwd == os.getcwd() and environment == dict(os.environ)
    assert all(spec.risk == "write" and not spec.concurrency_safe and not spec.replay_safe for spec in model.specs)


def test_unknown_class_method_and_invalid_arguments_fail_before_state_changes(tmp_path):
    case = filesystem_case()
    case["involved_classes"] = ["WebSearchAPI"]
    with pytest.raises(BFCLExecutionUnsupported):
        create_environment(case, root=tmp_path)
    environment = create_environment(filesystem_case(), root=tmp_path)
    for name, args in (("_load_scenario", {}), ("open", {"file_name": "x"}), ("cat", {"file_name": 1})):
        with pytest.raises((ValueError, BFCLExecutionUnsupported)):
            environment.execute(name, args)
    assert environment.results == []


def test_numeric_precision_serialization_occurs_before_restoration(tmp_path):
    mpmath = pytest.importorskip("mpmath")
    from aster.evaluation.bfcl_v4_official import vendored_simulator_classes

    case = {"id": "precision", "category": "multi_turn_base", "initial_config": {}, "involved_classes": ["MathAPI"],
            "functions": [{"name": "logarithm", "description": "Compute local logarithm.", "parameters": {
                "type": "dict", "properties": {"value": {"type": "float"}, "base": {"type": "float"},
                                                  "precision": {"type": "integer"}},
                "required": ["value", "base", "precision"]}}]}
    original_mpmath, original_decimal = mpmath.mp.dps, getcontext().prec
    direct = vendored_simulator_classes(["MathAPI"])["MathAPI"]()
    try:
        expected = str(direct.logarithm(value=13, base=3, precision=37))
    finally:
        mpmath.mp.dps = original_mpmath
        getcontext().prec = original_decimal
    environment = create_environment(case, root=tmp_path)
    actual = environment.execute("logarithm", {"value": 13, "base": 3, "precision": 37})
    assert actual == expected
    assert mpmath.mp.dps == original_mpmath and getcontext().prec == original_decimal
