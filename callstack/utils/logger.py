"""Structured logging with per-component loggers."""

import logging
import sys


_CALLSTACK_HANDLER_ATTR = "_callstack_owned_handler"


def setup_logging(level: str = "INFO") -> None:
    """Configure logging for all callstack components."""
    fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    root = logging.getLogger("callstack")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    handler = next(
        (h for h in root.handlers if getattr(h, _CALLSTACK_HANDLER_ATTR, False)),
        None,
    )
    if handler is None:
        handler = logging.StreamHandler(sys.stderr)
        setattr(handler, _CALLSTACK_HANDLER_ATTR, True)
        root.addHandler(handler)
    elif isinstance(handler, logging.StreamHandler) and handler.stream is not sys.stderr:
        handler.setStream(sys.stderr)
    handler.setFormatter(fmt)

    root.propagate = False
