"""structlog с редактированием секретов."""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

_SECRET_KEYS = re.compile(
    r"(token|secret|password|api_key|apikey|authorization|cookie|credential)", re.I
)
_SECRET_VALUES = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]+"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}\b"),  # telegram bot token
    re.compile(r"ya29\.[A-Za-z0-9_\-\.]+"),  # google access token
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.=]+"),
]
REDACTED = "***"


def redact_text(text: str) -> str:
    for pattern in _SECRET_VALUES:
        text = pattern.sub(REDACTED, text)
    return text


def _redact(value: Any, key: str = "") -> Any:
    if key and _SECRET_KEYS.search(key) and not key.endswith(("_ref", "_type")):
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(_redact(v) for v in value)
    return value


def redact_processor(_logger: Any, _name: str, event: dict[str, Any]) -> dict[str, Any]:
    return {k: _redact(v, k) for k, v in event.items()}


def configure_logging(verbose: bool = False, json_output: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            redact_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        # sys.stderr берётся в момент записи: его могут подменять (CliRunner, pytest)
        logger_factory=lambda *_: structlog.PrintLogger(file=sys.stderr),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
