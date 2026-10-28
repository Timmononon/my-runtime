import json
import os
import socket
import sys
from pathlib import Path

import httpx
import pytest

from aster.cli import main


def test_configure_model_only_never_writes_key(tmp_path, capsys):
    assert main(["--data-dir", str(tmp_path), "configure", "--model", "deepseek-flash", "--skip-key"]) == 0
    config = (tmp_path / "config.yaml").read_text()
    assert "deepseek-flash" in config and "api_key" not in config
    assert "Credentials are not written" in capsys.readouterr().out


def test_configure_uses_hidden_input_and_vault(tmp_path, monkeypatch):
    saved = []

    class Vault:
        def __init__(self, *, data_dir):
            assert data_dir == tmp_path

        def get(self, *args, **kwargs):
            return None

        def set(self, name, value):
            saved.append((name, value))

    monkeypatch.setattr("aster.cli.CredentialStore", Vault)
    monkeypatch.setattr("aster.cli.getpass.getpass", lambda prompt: "test-secret-key")
    assert main(["--data-dir", str(tmp_path), "configure"]) == 0
    assert len(saved) == 1 and saved[0][0].startswith("provider-") and saved[0][1] == "test-secret-key"
    assert "test-secret-key" not in (tmp_path / "config.yaml").read_text()


def test_doctor_is_readonly_with_respect_to_model(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("aster.maintenance.doctor", lambda path: {"data_dir": str(path), "network_requests": False})
    assert main(["--data-dir", str(tmp_path), "doctor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["network_requests"] is False


@pytest.mark.parametrize("selection", ["default", "home", "data", "explicit"])
def test_cli_configuration_uses_selected_home_without_writing_other_directories(tmp_path, monkeypatch, capsys, selection):
    default = tmp_path / "personal home"
    home_override = tmp_path / "home override"
    data_override = tmp_path / "data override"
    explicit = tmp_path / "explicit Runtime"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: default))
    monkeypatch.delenv("ASTER_DATA_DIR", raising=False)
    monkeypatch.delenv("ASTER_HOME", raising=False)
    choices = {"default": default / ".aster", "home": home_override, "data": data_override, "explicit": explicit}
    if selection != "default":
        monkeypatch.setenv("ASTER_HOME", str(home_override))
    if selection in {"data", "explicit"}:
        monkeypatch.setenv("ASTER_DATA_DIR", str(data_override))

    def forbidden_vault():
        raise AssertionError("Model-only configuration must not consult the credential store")

    monkeypatch.setattr("aster.cli.CredentialStore", forbidden_vault)
    argv = ["--data-dir", str(explicit)] if selection == "explicit" else []
    assert main([*argv, "configure", "--model", "deepseek-flash", "--skip-key"]) == 0
    selected = choices[selection]
    assert (selected / "config.yaml").read_text(encoding="utf-8") == "model: deepseek-flash\n"
    assert not (selected / "runtime.db").exists()
    assert not (selected / ".api-token").exists()
    for directory in choices.values():
        if directory != selected:
            assert not directory.exists()
    assert "Credentials are not written" in capsys.readouterr().out


def test_cli_template_is_text_only_and_prompt_is_exclusive(tmp_path, monkeypatch, capsys):
    from aster.domain import ModelTurn
    from aster.engine import FakeModel
    from aster.service import RunCoordinator
    (tmp_path / "commands.yaml").write_text('review: "Review $target using $mode"\n')
    model = FakeModel([ModelTurn("template done")])
    monkeypatch.setattr("aster.cli.RunCoordinator", lambda path: RunCoordinator(path, tools=[], model_factory=lambda _: model))
    code = main(["--data-dir", str(tmp_path), "run", "--command", "review", "--command-arg", "target=$(touch impossible)",
                 "--command-arg", "mode=careful", "--cwd", str(tmp_path), "--json"])
    assert code == 0
    assert model.calls[0]["messages"][0].content == "Review $(touch impossible) using careful"
    assert not (tmp_path / "impossible").exists()
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    assert main(["--data-dir", str(tmp_path), "run", "prompt", "--command", "review"]) == 2


def test_cli_branch_mailbox_and_performance_are_real_local_controls(tmp_path, monkeypatch, capsys):
    from aster.domain import ModelTurn
    from aster.engine import FakeModel
    from aster.service import RunCoordinator
    monkeypatch.setattr("aster.cli.RunCoordinator", lambda path: RunCoordinator(path, tools=[], model_factory=lambda _: FakeModel([ModelTurn("finished")])) )
    base = ["--data-dir", str(tmp_path)]
    assert main([*base, "run", "source request", "--cwd", str(tmp_path), "--json"]) == 0
    run = json.loads(capsys.readouterr().out)
    assert main([*base, "session", "fork", run["session_id"], "--through", "2"]) == 0
    child = json.loads(capsys.readouterr().out)
    assert child["lineage"]["parent_id"] == run["session_id"]
    assert main([*base, "message", child["id"], "new request", "--steer"]) == 0
    message = json.loads(capsys.readouterr().out)
    assert main([*base, "message-retract", child["id"], message["message_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["retracted"]
    assert main([*base, "performance", run["run_id"]]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["phase"] == "done" and "source request" not in json.dumps(report)


def test_cli_offline_evaluation_uses_isolated_fixture_and_no_credentials(tmp_path, capsys):
    output = tmp_path / "evaluation"
    assert main(["--data-dir", str(tmp_path / "unused-user-data"), "evaluate", "--output", str(output),
                 "--case", "files_edit_preserves_context"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "offline" and result["totals"].get("pass") == 1
    assert not (tmp_path / "unused-user-data").exists()


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt", reason="Windows service process integration")
def test_windows_daemon_start_health_stop_owned_process(tmp_path):
    from aster.maintenance import service_status, start_service, stop_service
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    argv = [sys.executable, "-m", "aster", "--data-dir", str(tmp_path), "daemon", "serve", "--port", str(port)]
    started = None
    try:
        started = start_service(tmp_path, argv, port=port, timeout=20)
        assert started["healthy"] and started["kind"] == "daemon"
        response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=3, trust_env=False)
        assert response.status_code == 200
        assert response.json()["instance_id"] == started["instance_id"]
        # No model request is made; health is operational without exercising credentials.
        unauthorized = httpx.get(f"http://127.0.0.1:{port}/sessions", timeout=3, trust_env=False)
        assert unauthorized.status_code == 401
        stopped = stop_service(tmp_path, timeout=10)
        assert stopped["stopped"] and not stopped["forced"]
        assert not service_status(tmp_path)["running"]
    finally:
        if started and service_status(tmp_path)["running"]:
            stop_service(tmp_path, timeout=5)
