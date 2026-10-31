import json
import socket
from pathlib import Path

import pytest

from aster.cli import main, parser
from aster.evaluation import bfcl_v4_cli as cli


def memory_case():
    return {"id": "memory_kv_0-notetaker-0", "base_id": "memory_0-notetaker-0", "category": "memory", "variant": "kv",
            "question": [[{"role": "user", "content": "What city?"}]],
            "expected": ["Oslo"], "functions": []}


def test_v4_inspection_branches_before_runtime_credentials_or_network(tmp_path, monkeypatch, capsys):
    import aster.cli

    forbidden = tmp_path / "user-runtime"
    monkeypatch.setenv("ASTER_DATA_DIR", str(forbidden))

    def fail(*_args, **_kwargs):
        pytest.fail("Offline inspection must not start a Runtime, resolve credentials or connect")
    monkeypatch.setattr(aster.cli, "RunCoordinator", fail)
    monkeypatch.setattr(socket, "create_connection", fail)
    monkeypatch.setattr(cli, "inspect_corpus", lambda *_args, **_kwargs: {
        "mode": "offline_inspection", "base_task_count": 1, "configuration_cases": 1,
        "model_quality": "unsupported", "model_api_calls": 0, "official_score": False})
    assert main(["bfcl", "v4", "inspect", "--corpus-dir", str(tmp_path / "corpus")]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["model_api_calls"] == 0 and output["model_quality"] == "unsupported"
    assert not forbidden.exists()


def test_v4_parser_has_no_live_switch_and_old_commands_keep_their_contract():
    assert parser().parse_args(["bfcl", "inspect", "--data", "q", "--answers", "a"]).bfcl_action == "inspect"
    with pytest.raises(SystemExit):
        parser().parse_args(["bfcl", "v4", "verify", "--corpus-dir", "data", "--output", "report.json", "--live"])


def test_saved_replay_ignores_claimed_scores_and_state(tmp_path, monkeypatch):
    case = {"id": "irrelevance_0", "category": "irrelevance", "variant": None,
            "functions": [{"name": "unrelated"}], "expected": None}
    monkeypatch.setattr(cli, "load_corpus", lambda *_args, **_kwargs: [case])
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text(json.dumps({"id": case["id"], "calls": [{"name": "unrelated", "arguments": {}}],
        "result": "pass", "valid": True, "state": {"fabricated": "correct"}, "model_quality": "evaluated"}) + "\n", encoding="utf-8")
    report = cli.replay_inputs(tmp_path / "corpus", predictions)
    row = report["scoring_verification"]["results"][0]
    assert row["valid"] is False and row["result"] == "fail"
    assert report["model_quality"] == "unsupported" and report["model_api_calls"] == 0
    assert report["capability_aggregation"]["overall_accuracy"] is None
    assert "fabricated" not in json.dumps(report)


def test_saved_native_wire_alias_is_restored_only_from_trusted_case(tmp_path, monkeypatch):
    import hashlib

    name = "math.add_values"
    case = {"id": "simple_python_0", "category": "simple_python", "variant": None,
        "functions": [{"name": name, "description": "Add.", "parameters": {"type": "dict",
                       "properties": {"value": {"type": "integer"}}, "required": ["value"]}}],
        "expected": [{name: {"value": [7]}}]}
    monkeypatch.setattr(cli, "load_corpus", lambda *_args, **_kwargs: [case])
    predictions = tmp_path / "wire.jsonl"
    alias = "b_" + hashlib.sha256(name.encode()).hexdigest()[:24]
    predictions.write_text(json.dumps({"id": case["id"], "calls": [{"name": alias, "arguments": {"value": 7}}],
                                       "name_map": {alias: "untrusted_override"}}) + "\n", encoding="utf-8")
    report = cli.replay_inputs(tmp_path, predictions)
    assert report["scoring_verification"]["results"][0]["valid"] is True
    assert "untrusted_override" not in json.dumps(report)


@pytest.mark.parametrize("ids", [["unknown"], ["memory_kv_0-notetaker-0"] * 2])
def test_unknown_or_duplicate_prediction_ids_reject_the_replay(tmp_path, monkeypatch, ids):
    monkeypatch.setattr(cli, "load_corpus", lambda *_args, **_kwargs: [memory_case()])
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text("\n".join(json.dumps({"id": identifier, "answer": "Oslo"}) for identifier in ids), encoding="utf-8")
    with pytest.raises(ValueError, match="IDs"):
        cli.replay_inputs(tmp_path, predictions)


def test_report_paths_do_not_overwrite_corpus_predictions_or_user_runtime(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    predictions = tmp_path / "predictions.json"
    predictions.write_text("fixture", encoding="utf-8")
    for target in (corpus / "report.json", predictions, tmp_path / ".aster" / "report.json"):
        with pytest.raises(ValueError):
            cli._output_path(target, corpus, predictions=predictions)
    assert predictions.read_text(encoding="utf-8") == "fixture"


@pytest.mark.asyncio
async def test_verify_gold_boundary_passes_remain_separate_from_model_accuracy(tmp_path, monkeypatch):
    case = memory_case()
    case["category"], case["variant"], case["id"] = "web_search", "base", "web_search_base_0"
    monkeypatch.setattr(cli, "load_corpus", lambda *_args, **_kwargs: [case])
    monkeypatch.setattr(cli, "inspect_corpus", lambda *_args, **_kwargs: {"configuration_cases": 1})
    report = await cli.verify_corpus(tmp_path, coordinator_smoke=False)
    assert report["scoring_verification"]["totals"] == {"pass": 1}
    assert report["capability_accuracy"] is None and report["capability_aggregation"]["overall_accuracy"] is None
    assert report["model_quality"] == "unsupported" and report["model_api_calls"] == 0
    assert report["scoring_rules"]["adaptations"]


def test_cli_replay_rejects_existing_output_before_loading(tmp_path, monkeypatch, capsys):
    output = tmp_path / "report.json"
    output.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(cli, "load_corpus", lambda *_args, **_kwargs: pytest.fail("Must reject before loading"))
    assert main(["bfcl", "v4", "replay", "--corpus-dir", str(tmp_path / "corpus"),
                 "--predictions", str(tmp_path / "predictions.jsonl"), "--output", str(output)]) == 2
    assert output.read_text(encoding="utf-8") == "keep" and "rejected" in capsys.readouterr().err


def test_standalone_parser_uses_the_same_namespace():
    commands = __import__("argparse").ArgumentParser().add_subparsers(dest="bfcl_action", required=True)
    cli.add_parser(commands)
    assert cli._output_path(None, Path("unused")) is None


def test_known_dataset_conflict_requires_exact_validation_evidence():
    import copy

    from aster.evaluation.bfcl_v4_data import REVISION, SAMPLE_MANIFEST_SHA256

    case = {"id": "multi_turn_base_173", "provenance": {
        "revision": REVISION, "sample_manifest_sha256": SAMPLE_MANIFEST_SHA256,
        "question_sha256": "5b99750ae263d28dd997e74c8d37e4d245f37e8adc01d0515c40aef5fd7e3f60"},
        "functions": [{"name": "close_ticket", "parameters": {"properties": {"ticket_id": {"type": "integer"}}}}],
        "expected": [[], [], [], ["close_ticket(ticket_id='ticket_001')"]], "question": [[], [], [], []]}
    report = {"protocol_result": "fail", "forced_termination": False, "budget": {"stop_reason": None},
        "runs": [{"runtime_status": "completed"}] * 4,
        "scoring_verification": {"turns": [{"valid": True}] * 3 + [{"valid": False}]},
        "messages": [{"role": "assistant", "tool_calls": [{"id": "bad-ticket", "name": "close_ticket",
            "arguments": {"ticket_id": "ticket_001"}}]},
            {"role": "tool", "tool_call_id": "bad-ticket", "content": "Invalid arguments: 'ticket_001' is not of type 'integer'",
             "metadata": {"is_error": True, "error_category": "validation", "outcome": "not_executed"}}]}
    assert cli._expected_schema_rejection(case, report)["expected_rejection_verified"] is True
    for mutate in (lambda value: value["messages"][0]["tool_calls"][0].update(name="other"),
                   lambda value: value["messages"][1]["metadata"].update(error_category="permission"),
                   lambda value: value["budget"].update(stop_reason="exhausted"),
                   lambda value: value.update(forced_termination=True)):
        altered = copy.deepcopy(report)
        mutate(altered)
        assert cli._expected_schema_rejection(case, altered) is None
