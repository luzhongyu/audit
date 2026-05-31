"""Run one agent: open an OpenCode Server session, send a JSON input,
parse + schema-validate the final JSON output, and persist a JSONL
artifact of every message exchanged.

Uses the OpenCode Server HTTP API (opencode serve) instead of the
Claude Code Agent SDK.  Each call creates a short-lived session so
that a schema-validation failure can be followed up with a repair turn
inside the same conversation context.

API-error handling: the OpenCode server surfaces errors as HTTP
error codes or `is_error=true` in the response body.  We classify the
error and either retry with exponential backoff (transient) or raise
QuotaExhaustedError (terminal).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from audit.json_utils import extract_json, validate_schema
from audit.opencode_client import OpenCodeClient

log = logging.getLogger(__name__)


@dataclass
class AgentResult:
    payload: dict
    cost_usd: float | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_creation_tokens: int | None
    num_turns: int | None
    duration_ms: int | None
    session_id: str | None
    artifact_path: Path
    repair_used: bool
    raw_result_message: dict = field(default_factory=dict)


class AgentRunError(RuntimeError):
    """Schema validation failed after repair attempts (model produced
    parseable output that didn't match the schema)."""


class TransientAgentError(RuntimeError):
    """API returned a transient error (server overloaded, network issue).
    The agent call should be retried with backoff."""


class QuotaExhaustedError(RuntimeError):
    """The LLM provider has returned a quota / rate-limit error.
    Don't retry — abort the pipeline and let the user wait."""


_QUOTA_MARKERS = (
    "quota",
    "rate limit exceeded",
    "rate_limit",
    "usage limit reached",
    "out of credits",
    "insufficient_quota",
    "429",
)

_TRANSIENT_MARKERS = (
    "overloaded",
    "service unavailable",
    "temporarily unavailable",
    "api error: 503",
    "api error: 502",
    "api error: 504",
    "api error: 500",
    "api error: 529",
    "socket connection",
    "connection closed",
    "connection reset",
    "unexpectedly closed",
    "timeout",
    "timed out",
    "internal server error",
    "bad gateway",
    "no user message found",    # OpenCode server race condition
)


def _classify_api_error(text: str) -> tuple[str, type[RuntimeError]]:
    """Return (label, exception_class) for an is_error response."""
    t = (text or "").lower()
    if any(m in t for m in _QUOTA_MARKERS):
        return "quota_exhausted", QuotaExhaustedError
    if any(m in t for m in _TRANSIENT_MARKERS):
        return "transient", TransientAgentError
    # Default to transient — better to retry once than abort on classification miss.
    return "unknown_api_error", TransientAgentError


async def run_agent(
    *,
    stage: str,
    prompt_file: Path,
    user_input: dict,
    schema_file: Path,
    allowed_tools: list[str],
    model: str,
    cwd: Path,
    add_dirs: list[Path] | None = None,
    max_turns: int = 25,
    permission_mode: str = "acceptEdits",
    artifact_dir: Path,
    artifact_name: str,
    repair_attempts: int = 1,
    transient_retries: int = 3,
    transient_base_delay: float = 30.0,
    opencode_client: OpenCodeClient | None = None,
) -> AgentResult:
    """Run one agent, retrying transient API errors with exponential backoff.

    Uses the OpenCode Server API instead of the Claude Code Agent SDK.

    Raises ``QuotaExhaustedError`` if the LLM provider returns a
    quota/rate-limit error (caller should abort the run).
    Raises ``TransientAgentError`` if all backoff retries are exhausted.
    Raises ``AgentRunError`` if the model produced parseable output that
    doesn't match the schema even after repair turns.
    """
    last_exc: RuntimeError | None = None
    for attempt in range(transient_retries + 1):
        try:
            return await _run_agent_once(
                stage=stage,
                prompt_file=prompt_file,
                user_input=user_input,
                schema_file=schema_file,
                allowed_tools=allowed_tools,
                model=model,
                cwd=cwd,
                add_dirs=add_dirs,
                max_turns=max_turns,
                permission_mode=permission_mode,
                artifact_dir=artifact_dir,
                artifact_name=artifact_name,
                repair_attempts=repair_attempts,
                opencode_client=opencode_client,
            )
        except QuotaExhaustedError:
            raise
        except TransientAgentError as e:
            last_exc = e
            if attempt >= transient_retries:
                break
            delay = min(transient_base_delay * (2 ** attempt), 240.0)
            log.warning(
                "[%s/%s] transient API error (attempt %d/%d): %s — retrying in %.0fs",
                stage, artifact_name, attempt + 1, transient_retries + 1,
                str(e)[:160], delay,
            )
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


async def _run_agent_once(
    *,
    stage: str,
    prompt_file: Path,
    user_input: dict,
    schema_file: Path,
    allowed_tools: list[str],
    model: str,
    cwd: Path,
    add_dirs: list[Path] | None,
    max_turns: int,
    permission_mode: str,
    artifact_dir: Path,
    artifact_name: str,
    repair_attempts: int,
    opencode_client: OpenCodeClient | None = None,
) -> AgentResult:
    """Single attempt via the OpenCode Server HTTP API.

    Raises TransientAgentError / QuotaExhaustedError before schema
    validation if the API returned an error.
    """
    client = opencode_client or OpenCodeClient(directory=str(cwd))

    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"{artifact_name}.jsonl"
    cwd.mkdir(parents=True, exist_ok=True)

    log.info(
        "[%s/%s] starting agent: model=%s max_turns=%d tools=%d",
        stage, artifact_name, model, max_turns, len(allowed_tools),
    )

    system_prompt = prompt_file.read_text()
    schema_text = schema_file.read_text()
    system_prompt += (
        "\n\n# Output schema\n\n"
        "Your output MUST validate against this JSON Schema. "
        "Pay attention to nested objects, required fields, and "
        "`additionalProperties: false`.\n\n"
        f"```json\n{schema_text}\n```\n"
    )

    initial_prompt = json.dumps(user_input, ensure_ascii=False)

    last_text = ""
    last_result_msg: dict[str, Any] = {}
    repair_used = False

    with artifact_path.open("w") as art:
        _write_artifact(art, {
            "kind": "meta", "stage": stage, "model": model,
            "started_at": time.time(),
        })
        _write_artifact(art, {"kind": "user", "text": initial_prompt[:50000]})

        session = await client.agent_session(
            model=model,
            tools=allowed_tools,
            title=f"{stage}/{artifact_name}",
            directory=str(cwd),
        )
        async with session:
            # --- initial prompt ---
            t0 = time.time()
            msg = await session.send(initial_prompt, system=system_prompt)
            elapsed = (time.time() - t0) * 1000
            _write_artifact(art, _serialize_msg(msg))
            last_text = msg.text
            last_result_msg = _msg_to_dict(msg)
            usage = msg.usage or {}
            log.info(
                "[%s/%s] agent responded in %.0fms: model=%s turns=%d "
                "input=%d output=%d cost=$%.4f is_error=%s",
                stage, artifact_name, elapsed, msg.model, msg.num_turns or 0,
                usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                msg.total_cost_usd or 0.0, msg.is_error,
            )

            # Before schema validation: was this a real model response, or
            # did the server surface an API error?
            if msg.is_error:
                label, exc_cls = _classify_api_error(last_text)
                _write_artifact(art, {
                    "kind": "api_error", "classification": label,
                    "text": last_text[:1000],
                })
                raise exc_cls(
                    f"[{stage}/{artifact_name}] {label}: "
                    f"{(last_text or '').strip()[:300]}"
                )

            # --- repair loop ---
            attempts = 0
            errors = _validate(last_text, schema_file, cwd=cwd)
            if errors:
                log.info(
                    "[%s/%s] schema validation failed, attempting repair (max %d): %s",
                    stage, artifact_name, repair_attempts, errors[:3],
                )
            while errors and attempts < repair_attempts:
                attempts += 1
                repair_used = True
                repair_prompt = _build_repair_prompt(last_text, errors, schema_file)
                log.info(
                    "[%s/%s] repair turn %d/%d …",
                    stage, artifact_name, attempts, repair_attempts,
                )
                _write_artifact(art, {
                    "kind": "repair_request", "text": repair_prompt[:50000],
                })
                msg = await session.send(repair_prompt)
                _write_artifact(art, _serialize_msg(msg))
                last_text = msg.text
                last_result_msg = _msg_to_dict(msg)

                if msg.is_error:
                    label, exc_cls = _classify_api_error(last_text)
                    _write_artifact(art, {
                        "kind": "api_error_on_repair", "classification": label,
                        "text": last_text[:1000],
                    })
                    raise exc_cls(
                        f"[{stage}/{artifact_name}] {label} on repair turn: "
                        f"{(last_text or '').strip()[:300]}"
                    )
                errors = _validate(last_text, schema_file, cwd=cwd)

            if errors:
                _write_artifact(art, {"kind": "schema_errors", "errors": errors})
                log.warning(
                    "[%s/%s] schema still invalid after %d repair turns: %s",
                    stage, artifact_name, repair_attempts, errors[:3],
                )
                raise AgentRunError(
                    f"[{stage}/{artifact_name}] schema validation failed after "
                    f"{repair_attempts} repair attempts: {errors[:5]}"
                )

            if repair_used:
                log.info(
                    "[%s/%s] schema validated after %d repair turn(s)",
                    stage, artifact_name, attempts,
                )

        payload = extract_json(last_text, cwd=cwd)
        _write_artifact(art, {"kind": "final_payload", "payload": payload})

    usage = last_result_msg.get("usage") or {}
    return AgentResult(
        payload=payload,
        cost_usd=last_result_msg.get("total_cost_usd"),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cache_read_tokens=usage.get("cache_read_input_tokens"),
        cache_creation_tokens=usage.get("cache_creation_input_tokens"),
        num_turns=last_result_msg.get("num_turns"),
        duration_ms=last_result_msg.get("duration_ms"),
        session_id=last_result_msg.get("session_id"),
        artifact_path=artifact_path,
        repair_used=repair_used,
        raw_result_message=last_result_msg,
    )


def _validate(text: str, schema_file: Path, *, cwd: Path | None = None) -> list[str]:
    try:
        payload = extract_json(text, cwd=cwd)
    except ValueError as e:
        return [f"json_extract: {e}"]
    return validate_schema(payload, schema_file)


def _build_repair_prompt(prev_output: str, errors: list[str], schema_file: Path) -> str:
    err_block = "\n".join(f"- {e}" for e in errors[:20])
    return (
        "Your previous output failed schema validation against "
        f"`{schema_file.name}`. Errors:\n"
        f"{err_block}\n\n"
        "Re-emit the same response, fixing ONLY these errors. Output a "
        "single JSON object — no prose, no markdown fence."
    )


def _write_artifact(fp, obj: Any) -> None:
    fp.write(json.dumps(obj, default=_json_fallback, ensure_ascii=False) + "\n")
    fp.flush()


def _json_fallback(o: Any) -> Any:
    if dataclasses.is_dataclass(o):
        return dataclasses.asdict(o)
    if isinstance(o, Path):
        return str(o)
    return repr(o)


def _serialize_msg(msg: Any) -> dict[str, Any]:
    """Serialize an OpenCodeMessage for JSONL artifact logging."""
    return {
        "kind": "assistant",
        "model": msg.model,
        "usage": msg.usage or {},
        "text": msg.text[:100000],
        "is_error": msg.is_error,
        "raw": msg.raw if msg.raw else {},
    }


def _msg_to_dict(msg: Any) -> dict[str, Any]:
    """Extract metadata dict from an OpenCodeMessage (like old _result_to_dict)."""
    return {
        "is_error": msg.is_error,
        "duration_ms": msg.duration_ms,
        "num_turns": msg.num_turns,
        "session_id": msg.session_id,
        "stop_reason": msg.stop_reason,
        "total_cost_usd": msg.total_cost_usd,
        "usage": msg.usage or {},
    }
