import sys

import pytest
import yaml

from aster.customization.credentials import CredentialStore
from aster.domain import ModelTurn
from aster.engine import FakeModel
from aster.service import RunCoordinator


@pytest.mark.asyncio
async def test_real_mcp_uses_explicit_runtime_credentials_instead_of_default_home(tmp_path, monkeypatch):
    default = tmp_path / "default home"
    selected = tmp_path / "isolated Runtime"
    selected.mkdir()
    monkeypatch.setenv("ASTER_HOME", str(default))
    monkeypatch.delenv("ASTER_DATA_DIR", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    CredentialStore(data_dir=default).set("mcp-account", "wrong-default-home-key")
    before = (default / "credentials.yaml").read_bytes()
    CredentialStore(data_dir=selected).set("mcp-account", "correct-isolated-key")
    server = tmp_path / "mcp_secret_fixture.py"
    server.write_text('''import os
from mcp.server.fastmcp import FastMCP
if os.environ.get("ACCESS_TOKEN") != "correct-isolated-key":
    raise RuntimeError("MCP received credentials from the wrong Runtime")
server = FastMCP("file-credential-isolation")
@server.tool()
def credential_check() -> str:
    return "credential accepted"
server.run(transport="stdio")
''', encoding="utf-8")
    (selected / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": [{
        "name": "secret-fixture", "command": sys.executable, "args": [str(server)],
        "env": {"ACCESS_TOKEN": "keyring:aster/mcp-account"}, "timeout_seconds": 10, "connect_attempts": 1}]}), encoding="utf-8")
    model = FakeModel([ModelTurn("offline fixture completed")])
    coordinator = RunCoordinator(selected, tools=[], model_factory=lambda _: model)
    try:
        sid = coordinator.create_session(tmp_path)["id"]
        run = await coordinator.submit(sid, "load the local MCP catalog")
        result = await coordinator.wait(run["id"], timeout=15)
        assert result.status == "completed", result.error
        assert len(model.calls) == 1
        snapshot = coordinator.store.run_request(run["id"])["snapshot"]
        assert snapshot["mcp_servers"][0]["env"] == {"ACCESS_TOKEN": "keyring:aster/mcp-account"}
        assert "correct-isolated-key" not in str(snapshot)
        assert (default / "credentials.yaml").read_bytes() == before
    finally:
        await coordinator.close()
