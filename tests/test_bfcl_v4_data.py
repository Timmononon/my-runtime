import copy
import hashlib
import json
import socket

import pytest

from aster.evaluation import bfcl_v4_data as data


def document(name):
    return {"name": name, "description": "Read a value.", "parameters": {
        "type": "dict", "properties": {"value": {"type": "integer"}}, "required": []}}


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    root = tmp_path / "coverage"
    root.mkdir()
    sources = []

    def source(relative, rows):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = ("\n".join(json.dumps(row) for row in rows) + "\n").encode()
        path.write_bytes(payload)
        sources.append({"local_file": relative, "revision": data.REVISION,
                        "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
    for filename in data.FUNCTION_DOCUMENTS.values():
        source(data.DATA_PREFIX + "multi_turn_func_doc/" + filename, [document("read"), document("later")])
    prereqs = [{"id": f"memory_prereq_{index}-customer-{index}", "scenario": "customer",
                "involved_classes": ["MemoryAPI"],
                "question": [[{"role": "user", "content": f"Remember value {index}."}]]} for index in range(2)]
    source(data.DATA_PREFIX + "memory_prereq_conversation/memory_customer.json", prereqs)
    rows = {
        "live_simple": [{"id": "live_simple_13-3-9", "question": [[{"role": "system", "content": "Read only."},
                          {"role": "user", "content": "Read value 7."}]], "function": [document("read")],
                          "private_extra": "must not enter public context"}],
        "multi_turn_miss_func": [{"id": "multi_turn_miss_func_0", "question": [
            [{"role": "user", "content": "Read."}], [], [{"role": "user", "content": "Use later."}]],
            "involved_classes": ["MathAPI"], "initial_config": {"private": "hidden"},
            "missed_function": {"1": ["later"]}, "excluded_function": ["unrelated"], "path": ["secret_oracle_path"]}],
        "memory": [{"id": "memory_0-customer-0", "scenario": "customer", "involved_classes": ["MemoryAPI"],
                    "question": [[{"role": "user", "content": "What value?"}]]}],
    }
    samples = []
    for track, questions in rows.items():
        answers = [{"id": row["id"], "ground_truth": ["seven"] if track == "memory" else []} for row in questions]
        files = []
        for kind, values in (("questions", questions), ("answers", answers)):
            relative = f"samples/{kind}/BFCL_v4_{track}.jsonl"
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = ("\n".join(json.dumps(row) for row in values) + "\n").encode()
            path.write_bytes(payload)
            files.append({"file": relative, "sha256": hashlib.sha256(payload).hexdigest()})
        samples.append({"track": track, "files": files, "question_ids": [row["id"] for row in questions],
                        "row_sha256": {row["id"]: hashlib.sha256(data._canonical(row)).hexdigest() for row in questions}})
    manifest = {"revision": data.REVISION, "samples": samples}
    provenance = {"revision": data.REVISION, "files": sources}
    for name, value, constant in (("sample-manifest.json", manifest, "SAMPLE_MANIFEST_SHA256"),
                                  ("provenance.json", provenance, "PROVENANCE_SHA256")):
        payload = json.dumps(value).encode()
        (root / name).write_bytes(payload)
        monkeypatch.setattr(data, constant, hashlib.sha256(payload).hexdigest())
    return root


def test_live_composite_id_and_system_are_preserved_without_network(corpus, monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: pytest.fail("No network allowed"))
    cases = data.load_corpus(corpus, tracks=["live_simple"])
    assert cases[0]["id"] == "live_simple_13-3-9"
    public = data.public_turn(cases[0], 0)
    assert public["system"] == "Read only."
    assert public["messages"] == [{"role": "user", "content": "Read value 7."}]
    assert "private_extra" not in json.dumps(public)


def test_missing_functions_reveal_on_the_exact_turn_and_oracle_stays_private(corpus):
    case = data.load_corpus(corpus, tracks=["multi_turn_miss_func"])[0]
    before = copy.deepcopy(case)
    first, reveal, last = (data.public_turn(case, index) for index in range(3))
    assert [function["name"] for function in first["functions"]] == ["read"]
    assert {function["name"] for function in reveal["functions"]} == {"read", "later"}
    assert reveal["messages"][0]["content"] == data.ADDITIONAL_FUNCTION_PROMPT
    assert reveal["functions"] == last["functions"]
    for public in (first, reveal, last):
        assert not {"expected", "initial_config", "path", "provenance"} & public.keys()
        assert "hidden" not in json.dumps(public) and "secret_oracle_path" not in json.dumps(public)
    assert case == before


def test_memory_expansion_contains_flat_complete_ordered_dependencies(corpus):
    cases = data.load_corpus(corpus, tracks=["memory"])
    assert [case["variant"] for case in cases] == ["kv", "vector", "rec_sum"]
    for case in cases:
        chain = case["memory_prerequisites"]
        assert len(chain) == 2 and all(entry["is_prerequisite"] for entry in chain)
        assert chain[0]["depends_on"] == []
        assert chain[1]["depends_on"] == [chain[0]["id"]]
        assert case["depends_on"] == [entry["id"] for entry in chain]
        assert all(entry["memory_prerequisites"] == [] for entry in chain)
        assert "seven" not in json.dumps(data.public_turn(case, 0))
        assert "respond in this format" in data.public_turn(case, 0)["system"]


@pytest.mark.parametrize("payload", ['{"a":1,"a":2}', '{"a":{"b":1,"b":2}}', '{"a":NaN}', '{"a":Infinity}', '{"a":1e999}'])
def test_strict_json_rejects_ambiguous_or_nonfinite_values(payload):
    with pytest.raises(ValueError):
        data.strict_json(payload)


@pytest.mark.parametrize("relative", ["sample-manifest.json", "provenance.json", "samples/questions/BFCL_v4_live_simple.jsonl",
                                      data.DATA_PREFIX + "multi_turn_func_doc/math_api.json"])
def test_changed_frozen_bytes_reject_the_whole_load_without_rewriting(corpus, relative):
    path = corpus / relative
    payload = path.read_bytes() + b" "
    path.write_bytes(payload)
    with pytest.raises(ValueError):
        data.load_corpus(corpus)
    assert path.read_bytes() == payload


def test_inspection_is_counts_only_and_never_claims_capability(corpus):
    report = data.inspect_corpus(corpus)
    assert report["base_task_count"] == 3 and report["configuration_cases"] == 5
    assert report["model_api_calls"] == 0 and report["model_quality"] == "unsupported"
    assert report["full_score_status"] == "incomplete_not_run"
    assert "seven" not in json.dumps(report) and "hidden" not in json.dumps(report)


def test_unknown_track_and_path_escape_are_rejected(corpus):
    with pytest.raises(ValueError, match="track"):
        data.load_corpus(corpus, tracks=["made_up"])
    with pytest.raises(ValueError, match="relative"):
        data._local(corpus, "../outside.json")
