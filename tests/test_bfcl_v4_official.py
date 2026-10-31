import ast
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aster.evaluation import bfcl_v4_official as official
from aster.evaluation._vendor.bfcl_v4.eval_checker.agentic_eval.agentic_checker import agentic_checker
from aster.evaluation._vendor.bfcl_v4.eval_checker.ast_eval.ast_checker import ast_checker
from aster.evaluation._vendor.bfcl_v4.memory_paths import get_directory_structure_by_id


def function(name="directory.find", kind="string", required=True):
    return {"name": name, "description": "Find an item.", "parameters": {"type": "dict",
            "properties": {"value": {"type": kind}}, "required": ["value"] if required else []}}


def test_pinned_vendor_bytes_and_original_function_bodies_match():
    evidence = official.verify_vendor_sources()
    assert evidence == {"valid": True, "files": 21, "source_revision": official.PINNED_REVISION}
    root = Path(official.__file__).parent / "_vendor/bfcl_v4"
    manifest = json.loads((root / "SOURCE_MANIFEST.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        original = ast.parse((root / entry["source"]).read_text(encoding="utf-8"))
        adapted = ast.parse((root / entry["file"]).read_text(encoding="utf-8"))
        original_definitions = {node.name: node for node in original.body
                                if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
        for node in adapted.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                # Namespace imports and optional dependency annotations are
                # outside these bodies, so source rule/simulator ASTs match.
                assert ast.dump(node) == ast.dump(original_definitions[node.name]), entry["file"]
        assert hashlib.sha256((root / entry["source"]).read_bytes()).hexdigest() == entry["source_sha256"]


@pytest.mark.parametrize("prediction,expected_valid", [("A-b / C", True), ("wrong", False)])
def test_python_ast_alias_restoration_matches_official_direct_result(prediction, expected_valid):
    functions = [function()]
    expected = [{"directory.find": {"value": ["a b c"]}}]
    calls = [{"name": "wire_alias", "arguments": {"value": prediction}}]
    before = copy.deepcopy((functions, expected, calls))
    result = official.grade_single_turn(functions, expected, calls, name_map={"wire_alias": "directory.find"})
    direct = ast_checker(functions, [{"directory.find": {"value": prediction}}], expected,
                         official.Language.PYTHON, "simple_python", "aster")
    assert result["valid"] == direct["valid"] == expected_valid
    assert (functions, expected, calls) == before
    assert result["official_score"] is False


def test_optional_argument_omission_and_extra_unknown_argument_follow_pinned_ast_rule():
    expected = [{"directory.find": {"value": ["", "default"]}}]
    assert official.grade_single_turn([function(required=False)], expected, [{"directory.find": {}}])["valid"]
    result = official.grade_single_turn([function(required=False)], expected,
                                        [{"directory.find": {"unknown": "default"}}])
    assert result["valid"] is False
    assert result["error_type"] == "simple_function_checker:unexpected_param"


def test_parallel_ast_accepts_reordering_but_consumes_duplicate_calls():
    functions = [function()]
    expected = [{"directory.find": {"value": ["A"]}}, {"directory.find": {"value": ["B"]}}]
    result = official.grade_single_turn(functions, expected,
        [{"directory.find": {"value": "B"}}, {"directory.find": {"value": "A"}}], category="parallel")
    assert result["valid"]
    assert not official.grade_single_turn(functions, expected,
        [{"directory.find": {"value": "A"}}, {"directory.find": {"value": "A"}}], category="parallel")["valid"]


@pytest.mark.parametrize("language,kind,literal,expected", [
    ("java", "float", "3.14f", 3.14), ("java", "long", "123L", 123),
    ("javascript", "Bigint", "123n", 123), ("javascript", "Boolean", "true", True),
])
def test_java_js_use_original_language_literal_converters(language, kind, literal, expected):
    functions = [function(name="convert", kind=kind)]
    answer = [{"convert": {"value": [expected]}}]
    assert official.grade_single_turn(functions, answer, [{"convert": {"value": literal}}],
                                       language=language, category="simple_" + language)["valid"]
    assert not official.grade_single_turn(functions, answer, [{"convert": {"value": expected}}],
                                           language=language, category="simple_" + language)["valid"]


def test_ast_never_evaluates_prediction_expressions_and_rejects_nonfinite_json(tmp_path):
    marker = tmp_path / "must-not-exist"
    expression = f"__import__('pathlib').Path({str(marker)!r}).write_text('unexpected')"
    expected = [{"directory.find": {"value": ["safe"]}}]
    result = official.grade_single_turn([function()], expected, [{"directory.find": {"value": expression}}])
    assert not result["valid"] and not marker.exists()
    result = official.grade_single_turn([function(kind="float")], expected,
                                        [{"directory.find": {"value": float("inf")}}])
    assert result["error_type"] == "ast:invalid_decoded_prediction"


def test_official_agentic_match_is_word_bounded_containment_not_whole_message_equality():
    answer = "I found this: {'answer': 'April 1, 2024', 'context': 'verified'}"
    truth = ["April 1 2024"]
    assert official.grade_agentic_answer(answer, truth, "web_search_base")["valid"] == agentic_checker(answer, truth)["valid"]
    assert not official.grade_agentic_answer("the number is 170", ["70"], "web_search_base")["valid"]
    assert official.grade_agentic_answer("the number is 70.", ["70"], "web_search_base")["valid"]
    # The pinned source does not strip whitespace despite its docstring prose.
    assert not official.grade_agentic_answer("strawberry  matcha", ["strawberry matcha"], "memory_kv")["valid"]


def test_agentic_missing_final_message_and_raw_answer_row():
    assert official.grade_agentic_answer("", ["value"], "memory_rec_sum")["error_type"] == "agentic:no_last_message"
    assert official.grade_agentic_answer("value", {"ground_truth": ["value"]}, "memory_rec_sum")["valid"]


@pytest.mark.parametrize("category,calls,valid", [
    ("irrelevance", [], True), ("live_irrelevance", [{"irrelevant": {}}], False),
    ("live_relevance", [], False), ("live_relevance", [{"any_decoded_name": {}}], True),
    ("irrelevance", [{}], True),
])
def test_relevance_counts_successfully_decoded_calls_not_semantic_task_success(category, calls, valid):
    assert official.grade_relevance(calls, category)["valid"] is valid


def test_state_and_cumulative_response_both_required_private_attrs_ignored():
    model = SimpleNamespace(state={"file": "done"}, _private_log="extra exploration")
    reference = SimpleNamespace(state={"file": "done"}, _private_log="reference")
    assert official.check_multiturn_turn({"FS": model}, {"FS": reference}, ["old", "needed", "needed"],
                                         ["needed", "needed"], 1)["valid"]
    missing = official.check_multiturn_turn({"FS": model}, {"FS": reference}, ["needed"], ["needed", "needed"])
    assert missing["error_type"] == "multi_turn:execution_response_mismatch"
    model.state = {"file": "wrong"}
    mismatch = official.check_multiturn_turn({"FS": model}, {"FS": reference}, ["needed"], ["needed"])
    assert mismatch["error_type"] == "multi_turn:instance_state_mismatch"


def test_empty_ground_truth_turn_uses_official_irrelevance_rule():
    assert official.check_irrelevant_turn([], [])["valid"]
    assert not official.check_irrelevant_turn([["unnecessary_call()"]], [])["valid"]
    assert official.check_irrelevant_turn([["needed_call()"]], ["needed_call()"])["valid"]


def test_simulator_factory_only_admits_known_local_names():
    classes = official.vendored_simulator_classes(["GorillaFileSystem", "VehicleControlAPI"])
    assert set(classes) == {"GorillaFileSystem", "VehicleControlAPI"}
    for name in ("WebSearchAPI", "MemoryAPI_vector", "os", "../anything"):
        with pytest.raises(ValueError):
            official.vendored_simulator_classes([name])
    with pytest.raises(ValueError):
        official.memory_class("vector")
    methods = official.vendored_simulator_method_names(["GorillaFileSystem"])
    assert "ls" in methods["GorillaFileSystem"]
    assert "get_directory_structure" not in methods["GorillaFileSystem"]
    assert all(not name.startswith("_") for name in methods["GorillaFileSystem"])


def test_forced_termination_is_failed_even_when_partial_states_match():
    assert official.check_multiturn_completion([[]], [[]])["valid"]
    assert official.check_multiturn_completion([], [[]])["error_type"] == "multi_turn:force_terminated"
    assert official.check_multiturn_completion([[]], [[]], forced_terminated=True)["error_type"] == "multi_turn:force_terminated"


def test_memory_relative_paths_never_read_environment_or_admit_path_syntax(tmp_path, monkeypatch):
    monkeypatch.setenv("BFCL_PROJECT_ROOT", str(tmp_path / "unused-user-output"))
    assert get_directory_structure_by_id("memory_kv_prereq_0-student-0") == "agentic/memory/kv"
    assert get_directory_structure_by_id("memory_rec_sum_2-customer-4") == "agentic/memory/rec_sum"
    for value in ("../memory_kv_0", "memory_kv_/../escape", "memory_unknown_1"):
        with pytest.raises(ValueError):
            get_directory_structure_by_id(value)
    assert not (tmp_path / "unused-user-output").exists()


def test_recursive_summary_memory_writes_only_explicit_temporary_snapshot_root(tmp_path):
    root = tmp_path / "isolated"
    cls = official.memory_class("rec_sum")
    writer = cls()
    writer._load_scenario({"model_result_dir": root, "test_id": "memory_rec_sum_prereq_0-student-0", "scenario": "student"})
    assert writer.memory_append("The user likes strawberries.")["status"]
    writer._flush_memory_to_local_file()
    reader = cls()
    reader._load_scenario({"model_result_dir": root, "test_id": "memory_rec_sum_1-student-0", "scenario": "student"})
    assert "strawberries" in reader._dump_core_memory_to_context()
    assert all(path.resolve().is_relative_to(root.resolve()) for path in root.rglob("*") if path.is_file())


def test_kv_memory_uses_official_bm25plus_and_isolated_snapshot_chain(tmp_path):
    pytest.importorskip("rank_bm25")
    root = tmp_path / "isolated-kv"
    cls = official.memory_class("kv")
    writer = cls()
    writer._load_scenario({"model_result_dir": root, "test_id": "memory_kv_prereq_0-student-0", "scenario": "student"})
    assert writer.core_memory_add("favorite_drink", "strawberry matcha")["status"]
    assert writer.core_memory_add("backup_drink", "lemon tea")["status"]
    writer._flush_memory_to_local_file()
    reader = cls()
    reader._load_scenario({"model_result_dir": root, "test_id": "memory_kv_1-student-0", "scenario": "student"})
    assert "strawberry matcha" in reader._dump_core_memory_to_context()
    ranked = reader._similarity_search("strawberry", ["strawberry matcha", "lemon tea"])
    assert ranked["ranked_results"][0][1] == "strawberry matcha"


class FixtureEncoder:
    """Protocol-only deterministic vectors; this is not a real model."""

    model_id = "sentence-transformers/all-MiniLM-L6-v2"
    local_files_only = True
    dimension = 384

    def __init__(self):
        self.calls = 0

    def encode(self, texts, *, normalize_embeddings):
        import numpy as np

        assert normalize_embeddings is True
        self.calls += 1
        result = np.zeros((len(texts), self.dimension), dtype=np.float32)
        for index, text in enumerate(texts):
            result[index, 0 if "strawberry" in text.lower() else 1] = 1
        return result


def test_vector_context_admission_resets_without_importing_or_calling_a_matrix_backend():
    from aster.evaluation._vendor.bfcl_v4.vector_context import current_vector_encoder

    class OfflineEncoder:
        model_id = "sentence-transformers/all-MiniLM-L6-v2"
        local_files_only = True
        dimension = 384

        def encode(self, *_args, **_kwargs):
            pytest.fail("Context admission must not invoke an encoder")

    first, second = OfflineEncoder(), OfflineEncoder()
    with official.admitted_vector_encoder(first):
        assert current_vector_encoder() is first
        with official.admitted_vector_encoder(second):
            assert current_vector_encoder() is second
        assert current_vector_encoder() is first
    with pytest.raises(ValueError, match="no admitted"):
        current_vector_encoder()


def test_vector_encoder_admission_is_explicit_nonloading_and_resets_context():
    pytest.importorskip("numpy", reason="BFCL vector matrix validation requires the optional bfcl-vector stack")
    from aster.evaluation._vendor.bfcl_v4.vector_context import ENCODER, current_vector_encoder

    first, second = FixtureEncoder(), FixtureEncoder()
    with pytest.raises(ValueError, match="no admitted"):
        official.memory_class("vector")
    with official.admitted_vector_encoder(first):
        assert first.calls == 0 and current_vector_encoder() is first
        with official.admitted_vector_encoder(second):
            assert current_vector_encoder() is second
            assert ENCODER.encode(["strawberry"], normalize_embeddings=True).shape == (1, 384)
        assert current_vector_encoder() is first
    assert first.calls == 0 and second.calls == 1
    with pytest.raises(ValueError, match="no admitted"):
        ENCODER.encode(["must not invoke a model"], normalize_embeddings=True)
    first.local_files_only = False
    with (pytest.raises(ValueError, match="explicit matching offline"),
          official.admitted_vector_encoder(first)):
        raise AssertionError("Admission should reject before entering")


def test_vector_encoder_invalid_output_is_rejected_before_faiss():
    pytest.importorskip("numpy", reason="BFCL vector matrix validation requires the optional bfcl-vector stack")
    from aster.evaluation._vendor.bfcl_v4.vector_context import ENCODER

    class InvalidEncoder(FixtureEncoder):
        def encode(self, texts, *, normalize_embeddings):
            import numpy as np

            return np.full((len(texts), 384), float("inf"), dtype=np.float32)

    with official.admitted_vector_encoder(InvalidEncoder()), pytest.raises(ValueError, match="invalid vector matrix"):
        ENCODER.encode(["text"], normalize_embeddings=True)


def test_official_vectorstore_body_runs_real_faiss_with_explicit_fixture_encoder(tmp_path):
    if importlib.util.find_spec("faiss") is None:
        pytest.skip("optional faiss-cpu dependency unavailable")
    encoder = FixtureEncoder()
    root = tmp_path / "vector-fixture"
    with official.admitted_vector_encoder(encoder):
        cls = official.memory_class("vector")
        writer = cls()
        writer._load_scenario({"model_result_dir": root, "test_id": "memory_vector_prereq_0-student-0", "scenario": "student"})
        assert writer.core_memory_add("strawberry matcha") == {"id": 0}
        assert writer.core_memory_add("lemon tea") == {"id": 1}
        response = writer.core_memory_retrieve("strawberry", top_k=1)
        assert response["result"][0]["text"] == "strawberry matcha"
        writer._flush_memory_to_local_file()
        reader = cls()
        reader._load_scenario({"model_result_dir": root, "test_id": "memory_vector_1-student-0", "scenario": "student"})
        assert reader.core_memory_retrieve("strawberry", top_k=1)["result"][0]["id"] == 0
        assert encoder.calls > 0
    assert all(path.resolve().is_relative_to(root.resolve()) for path in root.rglob("*") if path.is_file())


def test_aggregation_is_hierarchical_and_diagnostics_do_not_change_it():
    rows = {name: {"accuracy": 0.5, "total_count": 5} for name in official._LEAVES}
    rows["simple_python"]["accuracy"] = 1
    rows["simple_java"]["accuracy"] = 0
    rows["simple_javascript"]["accuracy"] = 0
    rows["live_relevance"] = {"accuracy": 1, "total_count": 10000}
    report = official.aggregate_scores(rows)
    expected_non_live = ((1 / 3) + 0.5 + 0.5 + 0.5) / 4
    assert report["categories"]["non_live"]["accuracy"] == pytest.approx(expected_non_live)
    assert report["overall_accuracy"] == pytest.approx(0.1 * expected_non_live + 0.9 * 0.5)
    assert report["diagnostics_affect_overall"] is False and report["official_score"] is False


@pytest.mark.parametrize("quality", ["unsupported", "scripted", "fixture", "not_measured", None])
def test_missing_or_scripted_tracks_remain_unknown_instead_of_zero_or_renormalization(quality):
    report = official.aggregate_scores({"simple_python": {"accuracy": 1, "total_count": 5}})
    assert report["status"] == "incomplete" and report["overall_accuracy"] is None
    report = official.aggregate_scores([{"category": "simple_python", "result": "pass", "model_quality": quality}])
    assert report["leaf_metrics"]["simple_python"]["accuracy"] is None
    with pytest.raises(ValueError, match="Unscored"):
        official.aggregate_scores([{"category": "simple_python", "result": "not_scored"}])


def test_live_sample_count_rule_and_full_distribution_estimate_are_explicit():
    rows = {name: {"accuracy": 0.5, "total_count": 5} for name in official._LEAVES}
    for name in official._LIVE_COUNTS:
        rows[name]["accuracy"] = 1 if name == "live_multiple" else 0
    sampled = official.aggregate_scores(rows)
    estimated = official.aggregate_scores(rows, live_weighting="official_full_counts")
    assert sampled["categories"]["live"]["accuracy"] == 0.25
    assert estimated["categories"]["live"]["accuracy"] == pytest.approx(1053 / 1351)
    assert sampled["live_weighting"] != estimated["live_weighting"]
