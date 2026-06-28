import io
import logging

from callstack.utils.logger import setup_logging


def _reset_callstack_logger():
    logger = logging.getLogger("callstack")
    original_handlers = list(logger.handlers)
    original_level = logger.level
    original_propagate = logger.propagate
    logger.handlers.clear()
    return logger, original_handlers, original_level, original_propagate


def _restore_callstack_logger(logger, handlers, level, propagate):
    logger.handlers.clear()
    logger.handlers.extend(handlers)
    logger.setLevel(level)
    logger.propagate = propagate


def test_repeated_setup_logging_emits_each_record_once(monkeypatch):
    logger, handlers, level, propagate = _reset_callstack_logger()
    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)

    try:
        setup_logging("INFO")
        setup_logging("INFO")
        logging.getLogger("callstack.test").info("probe once")

        rendered = [line for line in stream.getvalue().splitlines() if "probe once" in line]
        assert len(rendered) == 1
    finally:
        _restore_callstack_logger(logger, handlers, level, propagate)


def test_repeated_setup_logging_updates_level_without_duplicating_handlers(monkeypatch):
    logger, handlers, level, propagate = _reset_callstack_logger()
    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)

    try:
        setup_logging("INFO")
        setup_logging("DEBUG")

        assert logger.level == logging.DEBUG
        assert len(logger.handlers) == 1
    finally:
        _restore_callstack_logger(logger, handlers, level, propagate)


def test_repeated_setup_logging_rebinds_owned_handler_to_current_stderr(monkeypatch):
    logger, handlers, level, propagate = _reset_callstack_logger()
    first_stream = io.StringIO()
    second_stream = io.StringIO()

    try:
        monkeypatch.setattr("sys.stderr", first_stream)
        setup_logging("INFO")
        monkeypatch.setattr("sys.stderr", second_stream)
        setup_logging("INFO")

        logging.getLogger("callstack.test").info("after stderr swap")

        assert "after stderr swap" not in first_stream.getvalue()
        assert "after stderr swap" in second_stream.getvalue()
        assert len(logger.handlers) == 1
    finally:
        _restore_callstack_logger(logger, handlers, level, propagate)


def test_setup_logging_preserves_user_handlers(monkeypatch):
    logger, handlers, level, propagate = _reset_callstack_logger()
    user_stream = io.StringIO()
    user_handler = logging.StreamHandler(user_stream)
    logger.addHandler(user_handler)
    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)

    try:
        setup_logging("INFO")
        setup_logging("INFO")

        assert user_handler in logger.handlers
        assert len(logger.handlers) == 2
    finally:
        _restore_callstack_logger(logger, handlers, level, propagate)
