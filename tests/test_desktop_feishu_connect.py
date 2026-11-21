"""Exercise the human desktop setup path without a live CLI, account, or Windows keychain."""
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

script = Path(__file__).resolve().parents[1] / "desktop" / "feishu_connect.py"
spec = importlib.util.spec_from_file_location("desktop_feishu_connect", script)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class Transport:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.profile = SimpleNamespace(
            app_id="cli_example", brand="feishu", generation="revision", user_open_id="ou_example",
            executable=data_dir / "managed-cli.exe",
        )
        self.config = data_dir / "feishu-cli" / "config.json"
        self.config.parent.mkdir()
        self.config.write_text("original profiles and groups", encoding="utf-8")
        self.bot = {"available": False, "status": "not_configured",
                    "message": "Bot identity: not configured (missing app secret or bot token)"}
        self.user = {"available": False, "openId": "ou_example", "status": "missing"}
        self.strict = "off"
        self.calls = []

    def _profile(self):
        return self.profile

    def _require_generation(self, generation):
        assert generation == self.profile.generation

    async def _verify_version(self, profile):
        self.calls.append("verify-version")
        assert profile is self.profile

    def _environment(self):
        return {"LARKSUITE_CLI_CONFIG_DIR": str(self.config.parent)}

    async def _command(self, profile, *args):
        self._require_generation(profile.generation)
        if args == ("config", "strict-mode"):
            return f"strict-mode: {self.strict} (source: profile)".encode()
        assert args == ("auth", "status")
        return json.dumps({"credentialSource": "local", "appId": profile.app_id, "brand": profile.brand,
                           "identities": {"bot": self.bot, "user": self.user}, "dpopMode": "required"}).encode()

    async def discover(self):
        assert self.bot["available"]
        self.calls.append("discover")
        return {"identity": {"generation": self.profile.generation}, "groups": [{"name": "unprinted group"}]}


@pytest.fixture
def transport(tmp_path, monkeypatch):
    item = Transport(tmp_path)
    monkeypatch.setattr(setup, "FeishuEventTransport", lambda data_dir: item)
    monkeypatch.setattr(setup.subprocess, "run", lambda *a, **kw: pytest.fail("Unexpected native invocation"))
    monkeypatch.setattr(setup.httpx, "Client", lambda **kw: pytest.fail("Unexpected network invocation"))
    return item


async def test_missing_bot_stages_only_new_entry_and_returns_to_managed_profile(transport, monkeypatch, capsys):
    secret = "example-new-secret"
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: secret)
    probes = []
    monkeypatch.setattr(setup, "verify_entered_secret", lambda *values: probes.append(values))
    scratch = []

    def native(argv, **options):
        folder = Path(options["cwd"])
        scratch.append(folder)
        assert options["env"] == {"LARKSUITE_CLI_CONFIG_DIR": str(folder)}
        assert folder != transport.config.parent and list(folder.iterdir()) == []
        assert argv == [str(transport.profile.executable), "config", "init", "--app-id", "cli_example",
                        "--brand", "feishu", "--app-secret-stdin"]
        assert options["input"] == (secret + "\n").encode()
        assert options["stdout"] == options["stderr"] == subprocess.DEVNULL
        assert probes == [("cli_example", "feishu", secret)]
        transport.bot.update(available=True, status="ready")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(setup.subprocess, "run", native)
    await setup.connect(transport.data_dir, bot_only=True)
    assert not scratch[0].exists()
    assert transport.config.read_text(encoding="utf-8") == "original profiles and groups"
    assert transport.calls == ["verify-version", "discover"]
    out, err = capsys.readouterr()
    assert "1 个群" in out and secret not in out + err and "unprinted group" not in out + err


async def test_rejected_entry_is_never_saved(transport, monkeypatch):
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: "bad-entry")

    def reject(*values):
        raise setup.ConnectionError("未确认")

    monkeypatch.setattr(setup, "verify_entered_secret", reject)
    with pytest.raises(setup.ConnectionError, match="未确认"):
        await setup.connect(transport.data_dir, bot_only=True)
    assert transport.config.read_text(encoding="utf-8") == "original profiles and groups"


@pytest.mark.parametrize(("status", "subtype", "replace"), [
    (0, "", False), (1, "invalid_client", True), (1, "missing_scope", False), (1, "timeout", False),
])
async def test_ready_only_rebinds_on_typed_invalid_client(transport, monkeypatch, status, subtype, replace):
    transport.bot.update(available=True, status="ready")

    def bot_info(argv, **options):
        assert argv[1:] == ["api", "GET", "/open-apis/bot/v3/info", "--as", "bot", "--profile=cli_example"]
        assert options["env"]["LARKSUITE_CLI_CONFIG_DIR"] == str(transport.config.parent)
        error = json.dumps({"ok": False, "error": {"type": "config", "subtype": subtype}}).encode()
        return subprocess.CompletedProcess(argv, status, stdout=b"", stderr=error)

    monkeypatch.setattr(setup.subprocess, "run", bot_info)
    assert await setup.needs_bot_secret(transport, transport.profile) is replace


async def test_failed_identity_or_changed_generation_never_writes(transport, monkeypatch):
    transport.bot["message"] = "Bot identity disabled by current policy"
    with pytest.raises(setup.ConnectionError, match="身份策略"):
        await setup.connect_bot(transport, transport.profile)
    transport.user["openId"] = "ou_other"
    with pytest.raises(setup.ConnectionError, match="身份信息"):
        await setup.identity_status(transport, transport.profile)


@pytest.mark.parametrize(("brand", "host", "status", "body", "accepted"), [
    ("feishu", "accounts.feishu.cn", 200, {"access_token": "fixture"}, True),
    ("lark", "accounts.larksuite.com", 200, {"code": 0, "access_token": "fixture"}, True),
    ("feishu", "accounts.feishu.cn", 200, {"code": 0}, False),
    ("feishu", "accounts.feishu.cn", 200, {"error": "invalid_client", "code": 0}, False),
    ("feishu", "accounts.feishu.cn", 302, {}, False),
])
def test_candidate_secret_only_reaches_official_token_endpoint(monkeypatch, brand, host, status, body, accepted):
    real_client = httpx.Client

    def response(request):
        assert str(request.url) == f"https://{host}/oauth/v3/token"
        assert parse_qs(request.content.decode()) == {
            "grant_type": ["client_credentials"], "client_id": ["cli_fixture"], "client_secret": ["fixture-entry"],
        }
        return httpx.Response(status, json=body, headers={"Location": "https://unused.invalid/"})

    def client(**options):
        assert not options["follow_redirects"] and not options["trust_env"]
        return real_client(transport=httpx.MockTransport(response), **options)

    monkeypatch.setattr(setup.httpx, "Client", client)
    if accepted:
        setup.verify_entered_secret("cli_fixture", brand, "fixture-entry")
    else:
        with pytest.raises(setup.ConnectionError, match="未确认"):
            setup.verify_entered_secret("cli_fixture", brand, "fixture-entry")


@pytest.mark.parametrize("account", ["ou_example", "ou_other"])
async def test_oauth_uses_selected_scopes_without_replacing_original_binding(transport, monkeypatch, account):
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    def login(argv, **options):
        assert argv == [str(transport.profile.executable), "auth", "login", "--scope", "calendar:calendar:read",
                        "--profile=cli_example"]
        path = Path(options["env"]["LARKSUITE_CLI_CONFIG_DIR"]) / "config.json"
        assert str(path.parent) == options["cwd"] and path != transport.config
        config = json.loads(path.read_text(encoding="utf-8"))
        [app] = config["apps"]
        assert app == {"appId": "cli_example", "brand": "feishu", "strictMode": "off", "dpopMode": "required",
                       "appSecret": {"source": "keychain", "id": "appsecret:cli_example"}, "users": []}
        app["users"] = [{"userOpenId": account}]
        path.write_text(json.dumps(config), encoding="utf-8")
        if account == "ou_example":
            transport.user.update(available=True, scope="calendar:calendar:read")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(setup.subprocess, "run", login)
    if account == "ou_example":
        await setup.connect_user(transport, transport.profile, ["calendar:calendar:read"])
    else:
        with pytest.raises(setup.ConnectionError, match="账号不一致"):
            await setup.connect_user(transport, transport.profile, ["calendar:calendar:read"])
    assert transport.config.read_text(encoding="utf-8") == "original profiles and groups"


async def test_no_personal_oauth_without_explicit_consent_and_policy(transport, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    with pytest.raises(setup.ConnectionError, match="取消"):
        await setup.connect_user(transport, transport.profile, ["im:message:read"])
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    transport.strict = "bot"
    with pytest.raises(setup.ConnectionError, match="身份策略"):
        await setup.connect_user(transport, transport.profile, ["im:message:read"])
    transport.user.update(available=True, scope="im:message:read")
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("Unexpected personal authorization"))
    await setup.connect_user(transport, transport.profile, ["im:message:read"])


def test_scope_file_accepts_only_names_with_no_broad_default(tmp_path):
    source = tmp_path / "scopes.json"
    source.write_text('{"scopes":["im:message.send_as_user","offline_access","im:message.send_as_user"]}')
    assert setup.requested_scopes("", source) == ["im:message.send_as_user", "offline_access"]
    assert setup.requested_scopes("docs:document:read, offline_access") == ["docs:document:read", "offline_access"]
    for value in ("", "--domain all", "*"):
        with pytest.raises(setup.ConnectionError):
            setup.requested_scopes(value)


def test_menu_creates_independent_console_with_only_data_path_and_flags(tmp_path, monkeypatch):
    launches = []
    monkeypatch.setattr(setup.subprocess, "CREATE_NEW_CONSOLE", 16, raising=False)
    monkeypatch.setattr(setup.subprocess, "Popen", lambda argv, **kwargs: launches.append((argv, kwargs)))
    setup.launch_terminal(tmp_path, True)
    [(argv, options)] = launches
    assert argv == [setup.sys.executable, "-I", "-X", "utf8", str(script), "--data-dir", str(tmp_path),
                    "--pause", "--bot-only"]
    assert options == {"cwd": script.parent, "close_fds": True, "creationflags": 16}
