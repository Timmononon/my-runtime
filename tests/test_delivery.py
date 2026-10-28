"""Source/resource parity and package contamination fail closed before install."""
import importlib.util
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest
import yaml


def module(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


checker = module("verify_web_wheel")


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    (root / "src/aster/web/static/assets").mkdir(parents=True)
    (root / "frontend").mkdir()
    (root / "src/aster/__init__.py").write_text('__version__ = "0.1.1"\n', encoding="utf-8")
    (root / "src/aster/paths.py").write_text("# actual required source\n", encoding="utf-8")
    (root / "src/aster/resource.md").write_text("packaged workflow reference", encoding="utf-8")
    (root / "src/aster/web/static/index.html").write_text('<script src="/assets/app.js"></script>', encoding="utf-8")
    (root / "src/aster/web/static/assets/app.js").write_text("import('./settings.js')", encoding="utf-8")
    (root / "src/aster/web/static/assets/settings.js").write_text("export default true", encoding="utf-8")
    (root / "src/aster/web/static/assets/style.css").write_text("body{color:green}", encoding="utf-8")
    (root / "README.md").write_text("Exact delivery README.", encoding="utf-8")
    (root / "pyproject.toml").write_text('[project]\nname="aster-runtime"\nversion="0.1.1"\nrequires-python=">=3.12"\n', encoding="utf-8")
    (root / "frontend/package.json").write_text(json.dumps({"version": "0.1.1"}), encoding="utf-8")
    (root / "frontend/package-lock.json").write_text(json.dumps({"version": "0.1.1", "packages": {"": {"version": "0.1.1"}}}), encoding="utf-8")
    return root


def wheel(root, *, changes=None, omitted=()):
    entries = checker.source_inventory(root)
    metadata = "aster_runtime-0.1.1.dist-info/"
    entries[metadata + "METADATA"] = b"Metadata-Version: 2.3\nName: aster-runtime\nVersion: 0.1.1\nRequires-Python: >=3.12\n\nExact delivery README.\n"
    entries[metadata + "entry_points.txt"] = b"[console_scripts]\naster = aster.cli:main\n"
    entries.update(changes or {})
    for name in omitted:
        entries.pop(name)
    path = root / "fixture.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return path


def test_verifier_checks_all_python_resources_and_lazy_static(project):
    result = checker.verify_contents(wheel(project), project)
    assert result["python_files"] == 2 and result["package_files"] == 7
    assert result["static_files"] == 4


@pytest.mark.parametrize("name", ["aster/paths.py", "aster/resource.md", "aster/web/static/assets/settings.js"])
def test_missing_or_stale_source_and_resources_are_rejected(project, name):
    with pytest.raises(checker.DeliveryError, match="inventory"):
        checker.verify_contents(wheel(project, omitted=[name]), project)
    with pytest.raises(checker.DeliveryError, match="bytes"):
        checker.verify_contents(wheel(project, changes={name: b"stale"}), project)


@pytest.mark.parametrize("name", ["aster/credentials.yaml", "aster/runtime.db", "aster/web_fixture.py",
                                  "aster/web/static/node_modules/secret.js", "aster/../../outside.py"])
def test_forbidden_package_state_is_rejected_without_echo(project, name):
    marker = b"private-value-not-to-echo"
    with pytest.raises(checker.DeliveryError) as failure:
        checker.verify_contents(wheel(project, changes={name: marker}), project)
    assert marker.decode() not in str(failure.value)


def test_readme_and_frontend_version_must_match_the_same_source(project):
    package = wheel(project)
    (project / "README.md").write_text("Updated after wheel build", encoding="utf-8")
    with pytest.raises(checker.DeliveryError, match="README"):
        checker.verify_contents(package, project)
    (project / "frontend/package.json").write_text('{"version":"0.1.0"}', encoding="utf-8")
    with pytest.raises(checker.DeliveryError, match="versions"):
        checker.verify_contents(package, project)


def test_explicit_static_override_does_not_accept_historical_assets(project, tmp_path):
    fresh = tmp_path / "fresh"
    (fresh / "assets").mkdir(parents=True)
    (fresh / "index.html").write_text("fresh index", encoding="utf-8")
    (fresh / "assets/fresh.js").write_text("fresh script", encoding="utf-8")
    (fresh / "assets/fresh.css").write_text("fresh style", encoding="utf-8")
    expected = checker.source_inventory(project, fresh)
    assert "aster/web/static/assets/fresh.js" in expected
    assert "aster/web/static/assets/app.js" not in expected
    with pytest.raises(checker.DeliveryError, match="inventory"):
        checker.verify_contents(wheel(project), project, static_root=fresh)


def test_install_probe_environment_cannot_use_user_default_home_or_key(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "outside-secret")
    monkeypatch.setenv("ASTER_DATA_DIR", "outside-user-home")
    monkeypatch.setenv("ASTER_WEB_STATIC_ROOT", "historical-static")
    env = checker.isolated_environment(tmp_path / "install", tmp_path)
    assert "DEEPSEEK_API_KEY" not in env and "ASTER_DATA_DIR" not in env and "ASTER_WEB_STATIC_ROOT" not in env
    assert Path(env["ASTER_HOME"]).is_relative_to(tmp_path)
    assert env["PYTHONNOUSERSITE"] == "1"


def test_ci_builds_then_tests_then_verifies_the_same_assets():
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["tests"]["steps"]
    commands = "\n".join(step.get("run", "") for step in steps)
    assert commands.index("npm ci") < commands.index("npm test") < commands.index("npm run build")
    assert commands.index("ASTER_WEB_STATIC_ROOT=") < commands.index("pytest") < commands.index("build_delivery.py")
    assert "--static-root $env:ASTER_WEB_STATIC_ROOT" in commands and "verify_web_wheel.py" in commands
    assert 'node-version' in next(step["with"] for step in steps if step.get("uses", "").startswith("actions/setup-node"))


def test_current_package_inventory_includes_audit_modules():
    root = Path(__file__).resolve().parents[1]
    static = Path(os.environ.get("ASTER_WEB_STATIC_ROOT", root / "src/aster/web/static"))
    inventory = checker.source_inventory(root, static)
    for name in ("aster/paths.py", "aster/state/lifecycle.py", "aster/customization/management.py",
                 "aster/memory/semantic.py", "aster/service/browser_control.py"):
        assert name in inventory
    assert sys.version_info >= (3, 12)
