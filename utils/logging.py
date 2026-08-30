"""
utils/logging.py — Structured logging setup. Writes to stdout and DB.
Never logs secrets.
"""
from __future__ import annotations
import logging
import re
import sys
from typing import Optional

_SECRET_PATTERN = re.compile(
    r"(private_key|secret|passphrase|password|token|api_key|key_id)\s*[=:]\s*\S+",
    re.IGNORECASE,
)

_db_ref = None  # set after DB is initialized to avoid circular import


def set_db(db) -> None:
    global _db_ref
    _db_ref = db


class _ScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _SECRET_PATTERN.sub(r"\1=***REDACTED***", str(record.msg))
        return True


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)-20s %(message)s")
    )
    handler.addFilter(_ScrubFilter())
    root.addHandler(handler)


class DBHandler(logging.Handler):
    """Writes WARN+ logs to SQLite for dashboard visibility."""

    def emit(self, record: logging.LogRecord) -> None:
        if _db_ref is None:
            return
        try:
            msg = self.format(record)
            _db_ref.insert_log(record.levelname, msg[:1000])
        except Exception:
            pass


def add_db_handler() -> None:
    handler = DBHandler(level=logging.WARNING)
    handler.addFilter(_ScrubFilter())
    logging.getLogger().addHandler(handler)


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)
