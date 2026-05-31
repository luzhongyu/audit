"""HTTP client for the OpenCode Server API.

Replaces the Claude Code Agent SDK (claude-agent-sdk) with REST calls
to `opencode serve`.  One server handles many sessions; each agent
call creates a short-lived session, sends messages, and collects the
result.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_SERVER_URL = "http://127.0.0.1:4096"
_SERVER_START_TIMEOUT = 20.0

_server_lock = asyncio.Lock()
_server_started: dict[str, bool] = {}


class OpenCodeError(RuntimeError):
    pass


class OpenCodeServerNotRunning(OpenCodeError):
    pass


@dataclass
class OpenCodeMessage:
    """Structured representation of an assistant response."""

    text: str = ""
    model: str | None = None
    usage: dict | None = None
    total_cost_usd: float | None = None
    duration_ms: int | None = None
    num_turns: int | None = None
    session_id: str | None = None
    stop_reason: str | None = None
    is_error: bool = False
    raw: dict = field(default_factory=dict)


def _parse_message_response(response: dict) -> OpenCodeMessage:
    """Turn a /session/:id/message response into an OpenCodeMessage."""
    info = response.get("info", {})
    parts = response.get("parts", [])

    text_chunks: list[str] = []
    for p in parts:
        if p.get("type") == "text":
            text_chunks.append(p.get("text", ""))

    usage = info.get("tokens") or {}
    model_id = info.get("modelID", "")
    provider_id = info.get("providerID", "")
    return OpenCodeMessage(
        text="".join(text_chunks),
        model=f"{provider_id}/{model_id}" if provider_id else model_id,
        usage=usage,
        total_cost_usd=info.get("cost"),
        duration_ms=info.get("duration_ms"),
        num_turns=info.get("num_turns"),
        session_id=info.get("session_id"),
        stop_reason=info.get("finish"),
        is_error=info.get("is_error", False),
        raw=response,
    )


def _format_model(model_str: str) -> dict[str, str]:
    """Convert a model string like 'deepseek-v4-flash' or
    'deepseek/deepseek-v4-flash' to {providerID, modelID}."""
    if "/" in model_str:
        parts = model_str.split("/", 1)
        return {"providerID": parts[0], "modelID": parts[1]}
    return {"providerID": "deepseek", "modelID": model_str}


class OpenCodeClient:
    """Async HTTP client for an opencode server."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        auto_start: bool = True,
        directory: str | None = None,
    ):
        self.base_url = (
            base_url
            or os.environ.get("OPENCODE_SERVER_URL")
            or DEFAULT_SERVER_URL
        )
        self._auto_start = auto_start
        self._directory = directory
        headers = {}
        if directory:
            headers["x-opencode-directory"] = directory
        self._http = httpx.AsyncClient(
            base_url=self.base_url.rstrip("/"),
            timeout=httpx.Timeout(3600.0, connect=10.0),
            headers=headers,
        )
        self._server_process: asyncio.subprocess.Process | None = None

    async def __aenter__(self) -> OpenCodeClient:
        await self.ensure_running()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def ensure_running(self) -> None:
        """Check liveness; auto-start a server if configured."""
        try:
            await self.health()
        except OpenCodeServerNotRunning:
            if self._auto_start:
                await self._start_server()
            else:
                raise

    async def health(self) -> dict:
        try:
            resp = await self._http.get("/global/health", timeout=5.0)
            resp.raise_for_status()
            data = resp.json()
            log.debug("opencode server health OK: %s", data)
            return data
        except httpx.ConnectError as exc:
            log.debug("opencode server not reachable at %s", self.base_url)
            raise OpenCodeServerNotRunning(
                f"OpenCode server not reachable at {self.base_url}. "
                f"Start it with: opencode serve  (or set OPENCODE_SERVER_URL)"
            ) from exc

    async def _start_server(self) -> None:
        """Spawn `opencode serve` and wait for it to become ready.

        Uses a module-level lock so concurrent tasks don't race each
        other to start the server.
        """
        async with _server_lock:
            if _server_started.get(self.base_url):
                return

            log.info("starting opencode server (base_url=%s) …", self.base_url)
            port = DEFAULT_SERVER_URL.split(":")[-1]
            if ":" in self.base_url:
                port = self.base_url.rsplit(":", 1)[-1].rstrip("/")

            env = {
                **os.environ,
                "OPENCODE_CONFIG_CONTENT": '{"permission":{"external_directory":"allow"}}',
            }

            self._server_process = await asyncio.create_subprocess_exec(
                "opencode",
                "serve",
                "--port",
                port,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )

            deadline = time.monotonic() + _SERVER_START_TIMEOUT
            last_err = ""
            while time.monotonic() < deadline:
                try:
                    await self.health()
                    log.info("opencode server ready on %s", self.base_url)
                    _server_started[self.base_url] = True
                    return
                except OpenCodeServerNotRunning:
                    await asyncio.sleep(0.5)
                except httpx.HTTPError:
                    await asyncio.sleep(0.5)

            # Collect stderr for diagnostics
            if self._server_process.stderr:
                try:
                    stderr_data = await asyncio.wait_for(
                        self._server_process.stderr.read(), timeout=3.0
                    )
                    last_err = stderr_data.decode(errors="replace")[:500]
                except (asyncio.TimeoutError, OSError):
                    pass

            raise OpenCodeError(
                f"opencode server failed to start within {_SERVER_START_TIMEOUT}s "
                f"on {self.base_url}. stderr: {last_err}"
            )

    async def close(self) -> None:
        await self._http.aclose()
        if self._server_process is not None:
            try:
                self._server_process.terminate()
                await asyncio.wait_for(self._server_process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self._server_process.kill()
                    await self._server_process.wait()
                except ProcessLookupError:
                    pass
            self._server_process = None

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    async def create_session(self, title: str = "", *, directory: str | None = None) -> str:
        """Create a new session and return its id."""
        await self.ensure_running()
        params = {}
        dir_ = directory or self._directory
        if dir_:
            params["directory"] = dir_
        resp = await self._http.post(
            "/session",
            json={"title": title or "audit-agent"},
            params=params,
        )
        # 409 = session already exists; try to extract id, fallback to blank
        if resp.status_code == 409:
            try:
                body = resp.json()
                session_id = body.get("id", "")
                log.debug("session already exists: %s", session_id)
                return session_id
            except Exception:
                return ""
        resp.raise_for_status()
        body = resp.json()
        session_id = body.get("id", "")
        log.debug("session created: %s (title=%s, dir=%s)", session_id, title, dir_)
        return session_id

    async def delete_session(self, session_id: str) -> None:
        log.debug("deleting session: %s", session_id)
        try:
            await self._http.delete(f"/session/{session_id}")
        except httpx.HTTPError:
            pass

    # ------------------------------------------------------------------
    # Message / prompt
    # ------------------------------------------------------------------

    async def send_message(
        self,
        session_id: str,
        user_text: str,
        *,
        system: str | None = None,
        model: str | None = None,
        tools: list[str] | None = None,
    ) -> OpenCodeMessage:
        """Send a single user message and return the assistant response.

        The server automatically runs the tool-use loop and returns after
        the model emits its final text.
        """
        parts: list[dict] = [{"type": "text", "text": user_text}]
        body: dict[str, Any] = {"parts": parts}
        if system:
            body["system"] = system
        if model:
            body["model"] = _format_model(model)
        if tools:
            body["tools"] = {t: True for t in tools}

        log.debug(
            "sending message to session %s: model=%s tools=%s prompt_len=%d",
            session_id, model or "default", list(body.get("tools", {}).keys()),
            len(user_text),
        )
        t0 = time.time()
        resp = await self._http.post(
            f"/session/{session_id}/message",
            json=body,
        )
        elapsed = (time.time() - t0) * 1000

        # Convert HTTP errors to is_error messages so the runner's retry
        # logic can classify and handle them (transient / quota / terminal).
        if resp.status_code >= 400:
            try:
                error_body = resp.json()
            except Exception:
                error_body = {"error": resp.text[:500]}
            log.warning(
                "opencode HTTP %d in %.0fms: session=%s — %s",
                resp.status_code, elapsed, session_id,
                str(error_body)[:300],
            )
            return OpenCodeMessage(
                text=json.dumps(error_body) if isinstance(error_body, dict) else str(error_body),
                is_error=True,
                session_id=session_id,
                duration_ms=int(elapsed),
            )

        result = _parse_message_response(resp.json())
        log.debug(
            "message response in %.0fms: session=%s model=%s turns=%d "
            "input=%d output=%d is_error=%s",
            elapsed, session_id, result.model, result.num_turns or 0,
            (result.usage or {}).get("input_tokens", 0),
            (result.usage or {}).get("output_tokens", 0),
            result.is_error,
        )
        return result

    async def run_agent(
        self,
        user_text: str,
        *,
        system: str | None = None,
        model: str | None = None,
        session_title: str = "agent",
        directory: str | None = None,
    ) -> OpenCodeMessage:
        """Convenience: create session → send message → delete session."""
        session_id = await self.create_session(
            title=session_title, directory=directory
        )
        try:
            return await self.send_message(
                session_id,
                user_text,
                system=system,
                model=model,
            )
        finally:
            await self.delete_session(session_id)

    # ------------------------------------------------------------------
    # Session‑aware multi‑step (for repair turns)
    # ------------------------------------------------------------------

    @dataclass
    class AgentSession:
        client: OpenCodeClient
        session_id: str
        model: str | None = None
        tools: list[str] | None = None

        async def send(self, text: str, *, system: str | None = None) -> OpenCodeMessage:
            return await self.client.send_message(
                self.session_id,
                text,
                system=system,
                model=self.model,
                tools=self.tools,
            )

        async def close(self) -> None:
            await self.client.delete_session(self.session_id)

        async def __aenter__(self) -> OpenCodeClient.AgentSession:
            return self

        async def __aexit__(self, *args: Any) -> None:
            await self.close()

    async def agent_session(
        self,
        *,
        model: str | None = None,
        tools: list[str] | None = None,
        title: str = "agent",
        directory: str | None = None,
    ) -> AgentSession:
        """Create a reusable session (initial prompt + repair turns)."""
        session_id = await self.create_session(title=title, directory=directory)
        # Brief pause to let the server's session projector initialise
        # before we send the first message, avoiding a known race
        # condition where parts arrive "late" and are ignored.
        await asyncio.sleep(0.2)
        return OpenCodeClient.AgentSession(
            client=self,
            session_id=session_id,
            model=model,
            tools=tools,
        )
