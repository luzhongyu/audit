"""Tests for the API-error classification in runner.py."""

from __future__ import annotations

import pytest

from audit.runner import (
    QuotaExhaustedError,
    TransientAgentError,
    _classify_api_error,
)


@pytest.mark.parametrize("text", [
    "Insufficient quota · resets 2am (Europe/Rome)",
    "Usage limit reached for the day.",
    "quota exceeded for the current billing period",
    "429 Too Many Requests",
    "rate limit exceeded, please slow down",
    "rate_limit hit",
])
def test_quota_classified(text: str) -> None:
    label, exc = _classify_api_error(text)
    assert label == "quota_exhausted"
    assert exc is QuotaExhaustedError


@pytest.mark.parametrize("text", [
    "API Error: 529 Overloaded. This is a server-side issue, usually temporary",
    "Server overloaded — please try again",
    "API Error: 503",
    "API Error: 502 Bad Gateway",
        "API Error: 500 Internal Server Error",
        "Service temporarily unavailable",
])
def test_transient_classified(text: str) -> None:
    label, exc = _classify_api_error(text)
    assert label == "transient"
    assert exc is TransientAgentError


def test_unknown_defaults_to_transient() -> None:
    label, exc = _classify_api_error("some weird new error string")
    assert label == "unknown_api_error"
    assert exc is TransientAgentError


def test_empty_defaults_to_transient() -> None:
    label, exc = _classify_api_error("")
    assert label == "unknown_api_error"
    assert exc is TransientAgentError
