"""Console + file logging, one file per service, mirroring the operational
style of the existing BetPawa bot: every log line goes to both a rolling
file (for postmortems) and the terminal (for watching it live).
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path


def get_logger(service_name: str, log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(service_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger  # idempotent — safe to call more than once per process

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(name)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = logging.FileHandler(log_dir / f"{service_name}.log")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    return logger
