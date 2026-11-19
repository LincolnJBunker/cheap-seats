"""Logging configuration.

httpx logs every request URL at INFO. SerpApi requires its key as a query parameter, so those
loggers are pinned to WARNING to keep keys out of (public) GitHub Actions logs.
"""

from __future__ import annotations

import logging

QUIET_LOGGERS = ("httpx", "httpcore", "urllib3")


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
