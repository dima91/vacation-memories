"""Non-interactive logging: one timestamped line per record, no progress bars, no ANSI sequences."""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import TextIO

_HANDLER_MARK = "_vacation_memories_handler"


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="seconds")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=True)


def setup_logging(log_file: Path | None, log_format: str, quiet: bool, stream: TextIO | None = None) -> None:
    """Log everything to `log_file` (if any) and to the console (warnings only with `quiet`).

    Calling it again replaces the handlers installed by the previous call.
    """
    formatter = JsonFormatter() if log_format == "json" else TextFormatter()
    console = logging.StreamHandler(stream or sys.stderr)
    console.setLevel(logging.WARNING if quiet else logging.INFO)
    new_handlers: list[logging.Handler] = [console]
    if log_file is not None:
        new_handlers.append(logging.FileHandler(log_file, encoding="utf-8", errors="backslashreplace"))

    root = logging.getLogger()
    for handler in root.handlers[:]:
        if getattr(handler, _HANDLER_MARK, False):
            root.removeHandler(handler)
            handler.close()
    for handler in new_handlers:
        handler.setFormatter(formatter)
        setattr(handler, _HANDLER_MARK, True)
        root.addHandler(handler)
    root.setLevel(logging.INFO)
