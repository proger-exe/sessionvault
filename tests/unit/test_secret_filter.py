"""CI-тест: специально роняем секреты и убеждаемся, что фильтр их подавляет.

Из плана Этапа 1 — «CI-тест зловредно роняет секреты и падает, если они видны».
"""

from __future__ import annotations

import logging

import pytest

from sessionvault.security.secret_filter import SecretFilter, install


@pytest.fixture()
def isolated_logger() -> logging.Logger:
    logger = logging.getLogger("test.secret_filter")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self.format(record))


def _attach(logger: logging.Logger) -> ListHandler:
    handler = ListHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    install(logger)
    return handler


def test_redacts_phone_e164(isolated_logger: logging.Logger) -> None:
    handler = _attach(isolated_logger)
    isolated_logger.info("send code to +56933617390 via APP")
    assert "+56933617390" not in handler.records[0]
    assert "***REDACTED***" in handler.records[0]


def test_redacts_bare_login_code(isolated_logger: logging.Logger) -> None:
    handler = _attach(isolated_logger)
    isolated_logger.info("code=81215 received")
    assert "81215" not in handler.records[0]


def test_redacts_api_hash_and_master_key(isolated_logger: logging.Logger) -> None:
    handler = _attach(isolated_logger)
    isolated_logger.info("api_hash=0123456789abcdef0123456789abcdef loaded")
    assert "0123456789abcdef0123456789abcdef" not in handler.records[0]


def test_redacts_long_base64_auth_key(isolated_logger: logging.Logger) -> None:
    handler = _attach(isolated_logger)
    auth_key_b64 = "A" * 64
    isolated_logger.info("auth key persisted: %s", auth_key_b64)
    assert auth_key_b64 not in handler.records[0]


def test_redacts_sensitive_dict_arg(isolated_logger: logging.Logger) -> None:
    handler = _attach(isolated_logger)
    isolated_logger.info(
        "login attempt: %s",
        {"phone": "+56933617390", "code": "12345", "user_id": 42},
    )
    line = handler.records[0]
    assert "+56933617390" not in line
    assert "12345" not in line
    # user_id — не секрет, остаётся.
    assert "42" in line


def test_filter_leaves_non_sensitive_untouched(isolated_logger: logging.Logger) -> None:
    handler = _attach(isolated_logger)
    isolated_logger.info("account switched to state=authorized dc_id=1")
    assert "authorized" in handler.records[0]
    assert "dc_id=1" in handler.records[0]


def test_filter_survives_exception_args(isolated_logger: logging.Logger) -> None:
    """SecretFilter должен маскировать секреты в args исключений при exc_info."""
    handler = _attach(isolated_logger)
    try:
        raise ValueError("token=A" + "B" * 60 + " leaked")
    except ValueError:
        isolated_logger.exception("boom")
    body = "\n".join(handler.records)
    assert "AB" * 30 not in body


def test_direct_filter_class_use() -> None:
    """SecretFilter корректно инициализируется без install()."""
    flt = SecretFilter()
    record = logging.LogRecord(
        name="x", level=logging.INFO, pathname=__file__, lineno=1,
        msg="phone %s code %s", args=("+56900000000", 12345),
        exc_info=None,
    )
    flt.filter(record)
    assert "+56900000000" not in record.getMessage()
    assert "12345" not in record.getMessage()
