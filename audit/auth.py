"""Auth setup for the OpenCode Server API.

OpenCode handles its own authentication internally (opencode.json,
environment variables, /connect user flow, etc.).  This module simply
verifies that the `opencode` CLI is available and the server can be
reached.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

from audit.opencode_client import DEFAULT_SERVER_URL


@dataclass
class AuthStatus:
    opencode_cli_path: str | None
    opencode_cli_version: str | None
    server_reachable: bool
    server_version: str | None
    opencode_config_dir: Path | None


class AuthError(RuntimeError):
    pass


def _find_opencode_cli() -> str:
    """Return path to the opencode binary or raise AuthError."""
    path = shutil.which("opencode")
    if path is None:
        raise AuthError(
            "`opencode` CLI not found on PATH.\n"
            "Install it: curl -fsSL https://opencode.ai/install | bash\n"
            "Or via npm: npm install -g opencode-ai"
        )
    return path


def _get_opencode_version(cli_path: str) -> str | None:
    try:
        out = subprocess.run(
            [cli_path, "--version"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def _check_server(
    server_url: str | None = None,
) -> tuple[bool, str | None]:
    """Ping the server; return (reachable, version)."""
    url = server_url or os.environ.get("OPENCODE_SERVER_URL") or DEFAULT_SERVER_URL
    try:
        resp = httpx.get(f"{url.rstrip('/')}/global/health", timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            return True, data.get("version", "unknown")
    except (httpx.ConnectError, httpx.TimeoutException):
        pass
    return False, None


def configure_auth(
    env_file: Path | None = None,
    *,
    server_url: str | None = None,
) -> AuthStatus:
    """Verify that the OpenCode CLI is available and optionally check
    whether the server is reachable.

    Args:
        env_file: Optional .env file to load (via python-dotenv).
        server_url: Override for the opencode server URL.

    Returns an AuthStatus.  Raises AuthError if the CLI is missing.
    """
    if env_file is not None and env_file.exists():
        from dotenv import load_dotenv
        load_dotenv(env_file)
    else:
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

    cli_path = _find_opencode_cli()
    cli_version = _get_opencode_version(cli_path)

    server_reachable, server_version = _check_server(server_url)

    return AuthStatus(
        opencode_cli_path=cli_path,
        opencode_cli_version=cli_version,
        server_reachable=server_reachable,
        server_version=server_version,
        opencode_config_dir=_find_config_dir(),
    )


def _find_config_dir() -> Path | None:
    """Return the OpenCode config directory if it exists."""
    candidates = [
        Path.home() / ".config" / "opencode",
        Path.cwd() / ".opencode",
    ]
    for p in candidates:
        if p.is_dir():
            return p
    return None
