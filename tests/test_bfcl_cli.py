import json

import pytest

from aster.cli import main
from aster.evaluation.bfcl_cli import inspect_inputs


def inputs(tmp_path):
    data = tmp_path / "questions.jsonl"
    answers = tmp_path / "answers.jsonl"
    row = {"id": "simple_python_0", "question": [[{"role": "user", "content": "Calculate the sum of 2 and 3."}]],
           "function": [{"name": "sum_values", "description": "Add two numbers.", "parameters": {
               "type": "dict", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
               "required": ["a", "b"]}}]}
    data.write_text(json.dumps(row) + "\n", encoding="utf-8")
    answers.write_text(json.dumps({"id": row["id"], "ground_truth": [{"sum_values": {"a": [2], "b": [3]}}]}) + "\n", encoding="utf-8")
    return data, answers


def test_inspect_avoids_runtime_and_oracle_output(tmp_path, monkeypatch, capsys):
    data, answers = inputs(tmp_path)
    forbidden = tmp_path / "unused-user-runtime"
    monkeypatch.setenv("ASTER_DATA_DIR", str(forbidden))

    def fail(*args, **kwargs):
        raise AssertionError("Inspection must not instantiate a coordinator")

    monkeypatch.setattr("aster.cli.RunCoordinator", fail)
    assert main(["bfcl", "inspect", "--data", str(data), "--answers", str(answers)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["case_ids"] == ["simple_python_0"]
    assert result["official_score"] is False and result["model_quality"] == "unsupported"
    assert not forbidden.exists()
    assert "ground_truth" not in result and "question" not in result


def test_case_selection_does_not_silently_accept_missing_or_duplicate_ids(tmp_path):
    data, answers = inputs(tmp_path)
    for ids in (["missing"], [], ["simple_python_0", "simple_python_0"]):
        with pytest.raises(ValueError):
            inspect_inputs(data, answers, case_ids=ids)


def test_live_without_budget_and_explicit_credentials_is_rejected_before_dispatch(tmp_path, monkeypatch, capsys):
    data, answers = inputs(tmp_path)
    forbidden = tmp_path / "user-runtime"
    monkeypatch.setenv("ASTER_DATA_DIR", str(forbidden))
    assert main(["bfcl", "run", "--data", str(data), "--answers", str(answers),
                 "--output", str(tmp_path / "report.json"), "--live"]) == 2
    assert "--max-requests" in capsys.readouterr().err
    assert not forbidden.exists()
    assert not (tmp_path / "report.json").exists()


def test_inspection_cannot_overwrite_dataset(tmp_path, capsys):
    data, answers = inputs(tmp_path)
    before = data.read_bytes()
    assert main(["bfcl", "inspect", "--data", str(data), "--answers", str(answers), "--output", str(data)]) == 2
    assert data.read_bytes() == before
    assert "rejected" in capsys.readouterr().err


def test_legacy_evaluate_parser_is_preserved():
    from aster.cli import parser

    args = parser().parse_args(["evaluate", "--output", "legacy.json"])
    assert args.command == "evaluate" and args.live is False


def test_test_key_setup_is_explicit_hidden_and_never_echoed(tmp_path, monkeypatch, capsys):
    from aster.customization.credentials import CredentialStore

    target = tmp_path / "dedicated-key"
    monkeypatch.setattr("getpass.getpass", lambda prompt: "fixture-bfcl-secret")
    assert main(["bfcl", "configure-key", "--credential-data-dir", str(target)]) == 0
    assert CredentialStore(data_dir=target).get(use_environment=False) == "fixture-bfcl-secret"
    assert not (target / "runtime.db").exists()
    assert "fixture-bfcl-secret" not in capsys.readouterr().out


def test_key_setup_cannot_modify_existing_runtime(tmp_path, monkeypatch, capsys):
    target = tmp_path / "existing-runtime"
    target.mkdir()
    (target / "runtime.db").write_bytes(b"fixture-runtime")

    def fail(*args):
        raise AssertionError("Must reject before asking for a key")

    monkeypatch.setattr("getpass.getpass", fail)
    assert main(["bfcl", "configure-key", "--credential-data-dir", str(target)]) == 2
    assert not (target / "credentials.yaml").exists()
    assert (target / "runtime.db").read_bytes() == b"fixture-runtime"
    assert "rejected" in capsys.readouterr().err


def test_prepare_is_offline_before_runtime_provider_or_credential_resolution(tmp_path, monkeypatch, capsys):
    from aster.evaluation import bfcl_suite

    forbidden = tmp_path / "unused-user-runtime"
    monkeypatch.setenv("ASTER_DATA_DIR", str(forbidden))

    def fail(*args, **kwargs):
        raise AssertionError("Offline prepare must not instantiate a Runtime")

    monkeypatch.setattr("aster.cli.RunCoordinator", fail)
    observed = []

    def prepare(source, output, *, seed):
        observed.append((source, output, seed))
        return {"counts": {"all": 100, "dev": 40, "holdout": 60}}

    monkeypatch.setattr(bfcl_suite, "prepare_suite", prepare)
    assert main(["bfcl", "prepare", "--source-dir", str(tmp_path / "source"),
                 "--output-dir", str(tmp_path / "suite")]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["model_requests"] == 0 and result["counts"]["all"] == 100
    assert observed == [(tmp_path / "source", tmp_path / "suite", 20261029)]
    assert not forbidden.exists()
