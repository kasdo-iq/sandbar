"""data_api.ApiClient token resolution: ~/.secrets file first, env fallback second.

The sandbox token is read from `$HOME/.secrets/sandbox_api_token` (written by
/init-sandbox inside the container). Outside the container that file does not
exist, so SANDBOX_API_TOKEN is accepted as a fallback. The file keeps priority
so container behavior is unchanged.
"""

import pathlib

import pytest

from data_api import ApiClient


@pytest.fixture()
def fake_home(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    """Point Path.home() at an empty temp dir (no .secrets file)."""
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("MANUS_COMPAT", "true")
    return tmp_path


@pytest.mark.smoke
def test_token_file_wins_when_present(monkeypatch: pytest.MonkeyPatch, fake_home: pathlib.Path) -> None:
    secrets = fake_home / ".secrets"
    secrets.mkdir()
    (secrets / "sandbox_api_token").write_text("file-token\n", encoding="utf-8")
    monkeypatch.setenv("SANDBOX_API_TOKEN", "env-token")

    assert ApiClient().token == "file-token"


@pytest.mark.smoke
def test_env_fallback_when_file_missing(monkeypatch: pytest.MonkeyPatch, fake_home: pathlib.Path) -> None:
    monkeypatch.setenv("SANDBOX_API_TOKEN", "env-token")

    assert ApiClient().token == "env-token"


@pytest.mark.smoke
def test_raises_when_no_file_and_no_env(monkeypatch: pytest.MonkeyPatch, fake_home: pathlib.Path) -> None:
    monkeypatch.delenv("SANDBOX_API_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="SANDBOX_API_TOKEN is unset"):
        ApiClient()


@pytest.mark.smoke
def test_disabled_when_manus_compat_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANUS_COMPAT", "false")

    with pytest.raises(RuntimeError, match="MANUS_COMPAT=false"):
        ApiClient()
