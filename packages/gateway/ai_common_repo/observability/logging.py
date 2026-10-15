"""Structured logging with request/job correlation.

``configure_logging`` installs a JSON formatter plus a redacting filter on the
``ai_common_repo`` logger tree. Correlation ids (request-id, job-id) live in
contextvars and are injected into every record, so a line emitted deep inside a
handler still carries the id of the request or job that triggered it. Every
record's rendered message passes through the redactor before it is emitted, so a
stray secret in a log call never reaches the sink — values become hints, not text.
"""
from __future__ import annotations

import contextvars
import json
import logging
from contextlib import contextmanager

from ..core.ids import prefixed
from ..privacy.redactor import Redactor

LOGGER_ROOT = "ai_common_repo"

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "acr_request_id", default=None)
_job_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "acr_job_id", default=None)


# -- correlation context -----------------------------------------------------
def current_request_id() -> str | None:
    return _request_id.get()


def current_job_id() -> str | None:
    return _job_id.get()


@contextmanager
def request_context(request_id: str | None = None):
    """Bind a request-id for the duration of the block (HTTP middleware)."""
    rid = request_id or prefixed("req")
    token = _request_id.set(rid)
    try:
        yield rid
    finally:
        _request_id.reset(token)


@contextmanager
def job_context(job_id: str):
    """Bind a job-id for the duration of a worker handler invocation."""
    token = _job_id.set(job_id)
    try:
        yield job_id
    finally:
        _job_id.reset(token)


# -- filters / formatter -----------------------------------------------------
class _CorrelationFilter(logging.Filter):
    """Attach the current request/job ids to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get()
        record.job_id = _job_id.get()
        return True


class _RedactingFilter(logging.Filter):
    """Redact secrets from the already-rendered message. Runs after argument
    interpolation so values spliced via ``%s`` are covered too."""

    def __init__(self, redactor: Redactor):
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redactor.redact(record.getMessage())
        record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
            "job_id": getattr(record, "job_id", None),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(redactor: Redactor, *, level: str = "INFO",
                      fmt: str = "json") -> logging.Logger:
    """Install correlation + redaction on the package logger. Idempotent: a second
    call replaces the handler so reconfiguration (tests) does not stack outputs."""
    logger = logging.getLogger(LOGGER_ROOT)
    logger.setLevel(level.upper())
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if fmt == "json"
                         else logging.Formatter(
                             "%(asctime)s %(levelname)s [%(request_id)s/%(job_id)s] "
                             "%(name)s: %(message)s"))
    # Redact first (mutates msg), then stamp correlation ids onto the record.
    handler.addFilter(_RedactingFilter(redactor))
    handler.addFilter(_CorrelationFilter())
    logger.addHandler(handler)
    return logger
