"""Local-only logging. No handlers send data over the network."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Config


def setup_logging(config: Config, *, console: bool = True) -> logging.Logger:
    logs_dir = config.path("paths.logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    level_name = str(config.get("app.log_level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    # Avoid duplicate handlers during tests or repeated startup.
    for handler in list(root.handlers):
        if getattr(handler, "_pa_handler", False):
            root.removeHandler(handler)
            handler.close()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        logs_dir / "assistant.log",
        maxBytes=5_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler._pa_handler = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)
    if console:
        stream = logging.StreamHandler(stream=sys.stderr)
        stream.setFormatter(formatter)
        stream._pa_handler = True  # type: ignore[attr-defined]
        root.addHandler(stream)
    logging.captureWarnings(True)
    return logging.getLogger("personal_assistant")
