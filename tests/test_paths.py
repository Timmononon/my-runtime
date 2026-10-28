from pathlib import Path
from types import SimpleNamespace

import pytest

from aster.paths import resolve_data_dir


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "个人 Home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ASTER_DATA_DIR", raising=False)
    monkeypatch.delenv("ASTER_HOME", raising=False)
    return home


def test_default_home_resolution_has_no_filesystem_side_effects(isolated_home):
    assert resolve_data_dir() == isolated_home / ".aster"
    assert not isolated_home.exists()


@pytest.mark.parametrize("data_value,home_value,expected", [
    (None, None, ".aster"), ("", "", ".aster"), (None, "chosen-home", "chosen-home"),
    ("", "chosen-home", "chosen-home"), ("chosen-data", "chosen-home", "chosen-data"),
    ("chosen-data", "", "chosen-data"),
])
def test_environment_precedence_and_empty_values(isolated_home, monkeypatch, data_value, home_value, expected):
    if data_value is not None:
        monkeypatch.setenv("ASTER_DATA_DIR", str(isolated_home / data_value) if data_value else "")
    if home_value is not None:
        monkeypatch.setenv("ASTER_HOME", str(isolated_home / home_value) if home_value else "")
    assert resolve_data_dir() == isolated_home / expected
    assert not isolated_home.exists()


def test_explicit_directory_bypasses_environment_and_home_lookup(tmp_path, monkeypatch):
    class ForbiddenEnvironment:
        def get(self, *args):
            raise AssertionError("An explicit directory must not consult environment overrides")

    monkeypatch.setattr("aster.paths.os", SimpleNamespace(environ=ForbiddenEnvironment()))

    def forbidden_home(cls):
        raise AssertionError("An explicit directory must not consult the default home")

    monkeypatch.setattr(Path, "home", classmethod(forbidden_home))
    explicit = tmp_path / "isolated data"
    assert resolve_data_dir(explicit) == explicit.resolve()
    assert not explicit.exists()


@pytest.mark.parametrize("source", ["explicit", "ASTER_DATA_DIR", "ASTER_HOME"])
def test_selected_directory_expands_tilde(isolated_home, monkeypatch, source):
    value = "~/自定义 Aster"
    if source == "explicit":
        result = resolve_data_dir(value)
    else:
        monkeypatch.setenv(source, value)
        result = resolve_data_dir()
    assert result == isolated_home / "自定义 Aster"
    assert not isolated_home.exists()
