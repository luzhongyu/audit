"""Auth setup tests — OpenCode CLI availability + server reachability."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from audit.auth import AuthError, configure_auth


def _require_opencode_cli() -> None:
    if shutil.which("opencode") is None:
        pytest.skip("opencode CLI not installed")


# ---------- CLI absence ----------


def test_missing_opencode_cli_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(AuthError, match="opencode.*CLI"):
        configure_auth()


# ---------- successful auth ----------


def test_auth_finds_cli() -> None:
    _require_opencode_cli()
    status = configure_auth()
    assert status.opencode_cli_path is not None
    assert "opencode" in (status.opencode_cli_path or "")
    assert status.opencode_cli_version is not None


def test_auth_returns_config_dir() -> None:
    _require_opencode_cli()
    status = configure_auth()
    # At least one of the standard config dirs should exist if
    # the user has run opencode before.
    valid = [
        str(Path.home() / ".config" / "opencode"),
        str(Path.cwd() / ".opencode"),
    ]
    if status.opencode_config_dir:
        assert str(status.opencode_config_dir) in valid


def test_auth_server_check_does_not_crash() -> None:
    _require_opencode_cli()
    # Server may or may not be running — both are OK.
    status = configure_auth()
    assert isinstance(status.server_reachable, bool)


def test_auth_env_file_loading(tmp_path: Path) -> None:
    _require_opencode_cli()
    env_file = tmp_path / ".env"
    env_file.write_text("OPENCODE_SERVER_URL=http://localhost:9999\n")
    status = configure_auth(env_file=env_file)
    assert isinstance(status.server_reachable, bool)


def test_auth_server_url_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _require_opencode_cli()
    monkeypatch.setenv("OPENCODE_SERVER_URL", "http://localhost:9999")
    status = configure_auth()
    # Should not crash; server likely unreachable on :9999
    assert status.server_reachable is False
